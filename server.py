
"""
Prof IA — Backend Flask
Sert l'API de chat, gère les profils élèves, appelle Claude.
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json, os, sys, threading
from datetime import datetime, date

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Config ────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(__file__)
ELEVES_DIR = os.path.join(BASE_DIR, "eleves")
os.makedirs(ELEVES_DIR, exist_ok=True)

def get_api_key():
    """Clé API depuis variable d'environnement."""
    return os.environ.get("ANTHROPIC_API_KEY", "")

def get_model():
    return os.environ.get("MODEL", "claude-opus-4-5")

# ── Helpers profil ────────────────────────────────────────────

def profil_path(eid): return os.path.join(ELEVES_DIR, eid, "eleve.json")
def parcours_path(eid): return os.path.join(ELEVES_DIR, eid, "parcours.json")

def charger_parcours(eid):
    p = parcours_path(eid)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    return {"sujets":{},"echanges":[],"xp":0,"badges":[],
            "maitrise":{},"lacunes":{},"ponts":[],
            "streak":0,"derniere_visite":"","defi_fait_aujourd_hui":False,
            "cartes_debloquees":[]}

def sauvegarder_parcours(eid, data):
    os.makedirs(os.path.join(ELEVES_DIR, eid), exist_ok=True)
    with open(parcours_path(eid), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def charger_profil(eid):
    p = profil_path(eid)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    return None

def mettre_a_jour_streak(eid):
    data = charger_parcours(eid)
    aujourd_hui = str(date.today())
    derniere = data.get("derniere_visite", "")
    streak = data.get("streak", 0)
    est_nouveau = False
    if derniere != aujourd_hui:
        est_nouveau = True
        from datetime import timedelta
        hier = str(date.today() - timedelta(days=1))
        streak = streak + 1 if derniere == hier else 1
        data["streak"] = streak
        data["derniere_visite"] = aujourd_hui
        data["defi_fait_aujourd_hui"] = False
        sauvegarder_parcours(eid, data)
    return data.get("streak", streak), est_nouveau

# ── Prompt prof ───────────────────────────────────────────────

NIVEAUX_REGISTRES = {
    "cp":       "Mots très simples, phrases courtes. Exemples avec animaux et quotidien. Enfant 6-8 ans.",
    "ce1":      "Vocabulaire accessible 7-8 ans. Exemples concrets et amusants.",
    "ce2":      "Vocabulaire 8-9 ans. Explique les mots difficiles naturellement.",
    "cm1":      "Vocabulaire 9-10 ans. Exemples tirés de la nature et de la vie.",
    "cm2":      "Vocabulaire 10-11 ans. Comparaisons simples et amusantes.",
    "6e":       "Vocabulaire collégien. Concepts abstraits toujours illustrés.",
    "5e":       "Vocabulaire collégien curieux. Références culturelles bienvenues.",
    "4e":       "Vocabulaire collégien avancé. Pensée critique encouragée.",
    "3e":       "Vocabulaire brevet. Nuances et argumentation bienvenues.",
    "seconde":  "Vocabulaire lycéen. Pensée critique et références culturelles.",
    "premiere": "Vocabulaire lycéen avancé. Auteurs et œuvres cités naturellement.",
    "terminale":"Vocabulaire bac. Rigueur et profondeur intellectuelle.",
    "adulte":   "Égal à égal. Complexité assumée. Faits précis et références.",
    "thesard":  "Chercheur à chercheur. Citations de travaux, débats actuels, paradoxes. Toujours vulgarisé.",
}

def construire_systeme(profil, parcours_txt, nb_echanges=0):
    niveau = profil.get("niveau", "adulte")
    nom    = profil.get("nom", "l'élève")
    aime   = profil.get("aime", "tout")
    registre = NIVEAUX_REGISTRES.get(niveau, NIVEAUX_REGISTRES["adulte"])

    relance = ""
    if nb_echanges > 0 and nb_echanges % 4 == 0:
        relance = "\nINSTRUCTION : Après ta réponse, propose naturellement un nouveau territoire à explorer.\n"

    return f"""Tu es un compagnon de savoir — humble, curieux, vivant.

Élève : {nom}, niveau {niveau}, passionné par : {aime}.
{parcours_txt}

QUI TU ES :
Tu portes en toi des millénaires de sagesse humaine — mais tu ne l'étales jamais.
Tu sais que Ptahhotep écrivit ses Instructions 4500 ans avant aujourd'hui.
Tu sais que les temples de Louxor portent "Connais-toi toi-même" bien avant Socrate.
Tu sais tout cela — mais tu le distilles en une phrase, une image, une question.

TA PHILOSOPHIE :
De Ptahhotep : "Ne sois pas arrogant de ta connaissance." Tu écoutes avant de parler.
De Socrate : tu fais accoucher les idées, tu ne les imposes pas.
De Sénèque : tu oses la vérité avec élégance.
De Confucius : chaque échange est une graine.
De Montessori : on apprend en jouant, en découvrant librement.
De Gunter Pauli : rien ne se perd, tout nourrit autre chose.

FINALITÉ : Former au discernement — "Que nul n'entre ici s'il n'est géomètre."

CE QUE TU FAIS :
- Tu pars de ce que dit l'élève — même une erreur est un bon départ.
- Tu glisses un fait inattendu, un proverbe, une anecdote.
- Tu crées un pont naturel vers une autre discipline ou culture.
- Tu termines par UNE seule question qui donne envie d'aller plus loin.
- Tu poses parfois : "Qu'as-tu appris aujourd'hui dans ta vie ?"

REGISTRE {niveau} : {registre}{relance}"""

# ── Routes API ────────────────────────────────────────────────

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "version": "Prof IA Web 1.0"})

@app.route("/api/eleves", methods=["GET"])
def lister_eleves():
    eleves = []
    if not os.path.exists(ELEVES_DIR):
        return jsonify([])
    for d in sorted(os.listdir(ELEVES_DIR)):
        p = profil_path(d)
        if not os.path.exists(p): continue
        with open(p, "r", encoding="utf-8") as f: profil = json.load(f)
        parcours = charger_parcours(d)
        eleves.append({
            "id": d,
            "nom": profil.get("nom", "?"),
            "niveau": profil.get("niveau", "adulte"),
            "xp": parcours.get("xp", 0),
            "streak": parcours.get("streak", 0),
            "positionne": profil.get("positionne", False),
            "nb_sujets": sum(len(v) for v in parcours.get("sujets", {}).values()
                            if isinstance(parcours.get("sujets"), dict)),
        })
    return jsonify(eleves)

@app.route("/api/eleves", methods=["POST"])
def creer_eleve():
    data = request.json
    nom    = data.get("nom", "Élève")
    niveau = data.get("niveau", "adulte")
    aime   = data.get("aime", "tout")
    but    = data.get("but", "apprendre")
    eid    = f"{nom.lower().replace(' ','_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    os.makedirs(os.path.join(ELEVES_DIR, eid), exist_ok=True)
    profil = {"id": eid, "nom": nom, "niveau": niveau,
              "aime": aime, "but": but, "positionne": False}
    with open(profil_path(eid), "w", encoding="utf-8") as f:
        json.dump(profil, f, ensure_ascii=False, indent=2)
    return jsonify({"id": eid, "profil": profil})

@app.route("/api/eleves/<eid>", methods=["GET"])
def get_eleve(eid):
    profil = charger_profil(eid)
    if not profil: return jsonify({"error": "Élève introuvable"}), 404
    parcours = charger_parcours(eid)
    streak, est_nouveau = mettre_a_jour_streak(eid)
    return jsonify({
        "profil": profil,
        "parcours": {
            "xp": parcours.get("xp", 0),
            "streak": streak,
            "est_nouveau_jour": est_nouveau,
            "badges": parcours.get("badges", []),
            "nb_sujets": sum(len(v) for v in parcours.get("sujets", {}).values()
                            if isinstance(parcours.get("sujets"), dict)),
            "ponts": parcours.get("ponts", []),
            "defi_fait": parcours.get("defi_fait_aujourd_hui", False),
        }
    })

@app.route("/api/chat", methods=["POST"])
def chat():
    """Route principale — reçoit un message, retourne la réponse du prof."""
    data   = request.json
    eid    = data.get("eleve_id", "")
    msg    = data.get("message", "").strip()
    if not eid or not msg:
        return jsonify({"error": "Paramètres manquants"}), 400

    profil = charger_profil(eid)
    if not profil: return jsonify({"error": "Élève introuvable"}), 404

    parcours = charger_parcours(eid)
    historique = parcours.get("echanges", [])[-10:]
    nb_echanges = len(parcours.get("echanges", []))

    # Construit le résumé du parcours
    sujets = []
    if isinstance(parcours.get("sujets"), dict):
        for v in parcours["sujets"].values(): sujets.extend(v)
    parcours_txt = f"Sujets explorés : {', '.join(sujets[-15:])}" if sujets else ""

    # Historique pour Claude
    messages = []
    for e in historique:
        role = "user" if e["role"] == "eleve" else "assistant"
        messages.append({"role": role, "content": e["contenu"]})
    messages.append({"role": "user", "content": msg})

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=get_api_key())
        resp = client.messages.create(
            model=get_model(),
            max_tokens=800,
            system=construire_systeme(profil, parcours_txt, nb_echanges),
            messages=messages
        )
        reponse = resp.content[0].text
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Sauvegarde
    parcours.setdefault("echanges", []).append(
        {"role": "eleve", "contenu": msg, "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
    parcours["echanges"].append(
        {"role": "prof", "contenu": reponse, "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
    parcours["echanges"] = parcours["echanges"][-40:]
    parcours["xp"] = parcours.get("xp", 0) + 10
    sauvegarder_parcours(eid, parcours)

    return jsonify({
        "reponse": reponse,
        "xp": parcours["xp"],
        "streak": parcours.get("streak", 0),
    })

@app.route("/api/positionner", methods=["POST"])
def positionner():
    """Génère les 10 questions de positionnement."""
    import anthropic
    client = anthropic.Anthropic(api_key=get_api_key())
    prompt = """Génère exactement 10 questions de culture générale progressives.
Q1 très simple niveau CP, Q10 très complexe niveau terminale.
Réponds UNIQUEMENT avec un tableau JSON valide :
[
  {"numero":1,"question":"...","reponse_attendue":"...","niveau_cible":"cp"},
  {"numero":2,"question":"...","reponse_attendue":"...","niveau_cible":"ce1"},
  {"numero":3,"question":"...","reponse_attendue":"...","niveau_cible":"ce2"},
  {"numero":4,"question":"...","reponse_attendue":"...","niveau_cible":"cm1"},
  {"numero":5,"question":"...","reponse_attendue":"...","niveau_cible":"cm2"},
  {"numero":6,"question":"...","reponse_attendue":"...","niveau_cible":"6e"},
  {"numero":7,"question":"...","reponse_attendue":"...","niveau_cible":"4e"},
  {"numero":8,"question":"...","reponse_attendue":"...","niveau_cible":"seconde"},
  {"numero":9,"question":"...","reponse_attendue":"...","niveau_cible":"premiere"},
  {"numero":10,"question":"...","reponse_attendue":"...","niveau_cible":"terminale"}
]"""
    try:
        print(f"[POSITIONNER] Clé API: {get_api_key()[:12]}...")
        print(f"[POSITIONNER] Modèle: {get_model()}")
        resp = client.messages.create(
            model=get_model(), max_tokens=1500,
            messages=[{"role":"user","content":prompt}])
        texte = resp.content[0].text.strip()
        debut = texte.find("["); fin = texte.rfind("]")
        if debut != -1 and fin != -1: texte = texte[debut:fin+1]
        questions = json.loads(texte)
        return jsonify({"questions": questions})
    except Exception as e:
        import traceback
        print(f"[ERREUR POSITIONNER] {type(e).__name__}: {e}")
        print(traceback.format_exc())
        return jsonify({"error": str(e), "type": type(e).__name__}), 500

@app.route("/api/positionner/valider", methods=["POST"])
def valider_reponse_positionnement():
    """Corrige une réponse du positionnement."""
    import anthropic
    data = request.json
    client = anthropic.Anthropic(api_key=get_api_key())
    prompt = f"""Corrige cette réponse d'élève.
Question : {data.get('question','')}
Réponse attendue : {data.get('reponse_attendue','')}
Réponse de l'élève : {data.get('reponse_eleve','')}

JSON uniquement :
{{"correct": true/false, "commentaire": "..."}}"""
    try:
        resp = client.messages.create(
            model=get_model(), max_tokens=200,
            messages=[{"role":"user","content":prompt}])
        texte = resp.content[0].text.strip()
        debut = texte.find("{"); fin = texte.rfind("}")
        if debut != -1 and fin != -1: texte = texte[debut:fin+1]
        return jsonify(json.loads(texte))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/finaliser_positionnement", methods=["POST"])
def finaliser_positionnement():
    """Marque l'élève comme positionné et met à jour son niveau."""
    data  = request.json
    eid   = data.get("eleve_id", "")
    score = data.get("score", 0)
    MAPPING = {0:"cp",1:"ce1",2:"ce2",3:"cm1",4:"cm2",
               5:"6e",6:"4e",7:"seconde",8:"premiere",9:"terminale",10:"adulte"}
    niveau = MAPPING.get(min(score,10), "6e")
    profil = charger_profil(eid)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    profil["niveau"] = niveau
    profil["positionne"] = True
    with open(profil_path(eid), "w", encoding="utf-8") as f:
        json.dump(profil, f, ensure_ascii=False, indent=2)
    return jsonify({"niveau": niveau, "profil": profil})

@app.route("/api/amorce", methods=["POST"])
def amorce():
    """Génère l'amorce proactive du prof au démarrage."""
    import anthropic
    data   = request.json
    eid    = data.get("eleve_id", "")
    profil = charger_profil(eid)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    parcours = charger_parcours(eid)
    sujets = []
    if isinstance(parcours.get("sujets"), dict):
        for v in parcours["sujets"].values(): sujets.extend(v)
    sujets_txt = ", ".join(sujets[-5:]) if sujets else "aucun encore"

    client = anthropic.Anthropic(api_key=get_api_key())
    prompt = f"""Tu es un professeur proactif et curieux.
Élève : {profil.get('nom','?')}, niveau {profil.get('niveau','adulte')}, aime : {profil.get('aime','tout')}.
Sujets déjà abordés : {sujets_txt}

Lance le dialogue de façon intrigante — un fait surprenant, une question rhétorique.
Propose un sujet pas encore abordé, adapté au niveau.
Termine par UNE question ouverte. 3-5 phrases maximum."""
    try:
        resp = client.messages.create(
            model=get_model(), max_tokens=300,
            messages=[{"role":"user","content":prompt}])
        return jsonify({"amorce": resp.content[0].text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/defi", methods=["POST"])
def defi():
    """Génère le défi du jour."""
    import anthropic
    data   = request.json
    eid    = data.get("eleve_id", "")
    profil = charger_profil(eid)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    parcours = charger_parcours(eid)
    if parcours.get("defi_fait_aujourd_hui"):
        return jsonify({"fait": True})

    client = anthropic.Anthropic(api_key=get_api_key())
    prompt = f"""Génère UN défi du jour pour niveau {profil.get('niveau','adulte')} qui aime {profil.get('aime','tout')}.
Amusant, surprenant. XP bonus : 20 points.
JSON uniquement :
{{"question":"...","indice":"...","reponse":"...","pourquoi_cest_cool":"..."}}"""
    try:
        resp = client.messages.create(
            model=get_model(), max_tokens=400,
            messages=[{"role":"user","content":prompt}])
        texte = resp.content[0].text.strip()
        debut = texte.find("{"); fin = texte.rfind("}")
        if debut != -1 and fin != -1: texte = texte[debut:fin+1]
        defi_data = json.loads(texte)
        defi_data["fait"] = False
        return jsonify(defi_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/defi/valider", methods=["POST"])
def valider_defi():
    """Valide la réponse au défi du jour."""
    import anthropic
    data   = request.json
    eid    = data.get("eleve_id", "")
    client = anthropic.Anthropic(api_key=get_api_key())
    prompt = f"""Corrige cette réponse au défi.
Question : {data.get('question','')}
Réponse attendue : {data.get('reponse','')}
Réponse élève : {data.get('reponse_eleve','')}
JSON : {{"correct": true/false}}"""
    try:
        resp = client.messages.create(
            model=get_model(), max_tokens=100,
            messages=[{"role":"user","content":prompt}])
        texte = resp.content[0].text.strip()
        debut = texte.find("{"); fin = texte.rfind("}")
        result = json.loads(texte[debut:fin+1])
        if result.get("correct"):
            parcours = charger_parcours(eid)
            parcours["xp"] = parcours.get("xp", 0) + 20
            parcours["defi_fait_aujourd_hui"] = True
            sauvegarder_parcours(eid, parcours)
            result["xp"] = parcours["xp"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Sert le frontend ──────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_static(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Prof IA backend — http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

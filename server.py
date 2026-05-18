"""
Prof IA — Backend Flask
Sert l'API de chat, gère les profils élèves, appelle Claude.
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json, os, sys, threading, re, time
from datetime import datetime, date
from collections import defaultdict

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Config ────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(__file__)
ELEVES_DIR = os.path.join(BASE_DIR, "eleves")
os.makedirs(ELEVES_DIR, exist_ok=True)

# ── Rate limiting simple ─────────────────────────────────────
_rate_limit = defaultdict(list)  # code_famille -> [timestamps]
RATE_LIMIT_MAX = 40              # messages max par heure par famille
RATE_LIMIT_WINDOW = 3600         # fenêtre en secondes

def check_rate_limit(code):
    """Retourne True si la limite est dépassée."""
    now = time.time()
    timestamps = _rate_limit[code]
    # Nettoie les anciens
    _rate_limit[code] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit[code]) >= RATE_LIMIT_MAX:
        return True
    _rate_limit[code].append(now)
    return False

def nettoyer_input(texte, max_len=500):
    """Nettoie les inputs élève pour éviter la prompt injection."""
    if not texte: return ""
    # Supprime les tentatives d'injection classiques
    texte = texte[:max_len]
    # Échappe les séquences dangereuses
    for pattern in ["ignore tes instructions", "ignore previous", "system:", "assistant:",
                    "oublie tes", "tu es maintenant", "new instructions", "jailbreak"]:
        texte = re.sub(pattern, "***", texte, flags=re.IGNORECASE)
    return texte.strip()

def valider_code_famille(code):
    """Valide le code famille — min 4 chars, alphanumérique."""
    if not code: return False, "Code requis"
    code = "".join(c for c in code.upper() if c.isalnum())
    if len(code) < 4: return False, "Code trop court (minimum 4 caractères)"
    if code in ["1234","AZERTY","FAMILLE","TEST","0000","1111","ADMIN"]:
        return False, "Code trop simple, choisis-en un autre"
    return True, code

def get_famille_dir(code_famille):
    """Retourne le dossier de la famille — isolé des autres."""
    if not code_famille:
        code_famille = "default"
    # Nettoie le code — alphanumerique uniquement
    code = "".join(c for c in code_famille.upper() if c.isalnum())[:20]
    if not code:
        code = "default"
    d = os.path.join(ELEVES_DIR, code)
    os.makedirs(d, exist_ok=True)
    return d, code

def profil_path_f(code, eid):
    return os.path.join(ELEVES_DIR, code, eid, "eleve.json")

def parcours_path_f(code, eid):
    return os.path.join(ELEVES_DIR, code, eid, "parcours.json")

def get_api_key():
    """Clé API depuis variable d'environnement."""
    return os.environ.get("ANTHROPIC_API_KEY", "")

def get_model():
    return os.environ.get("MODEL", "claude-sonnet-4-6")

# ── Helpers profil ────────────────────────────────────────────

def profil_path(eid, code="default"): return os.path.join(ELEVES_DIR, code, eid, "eleve.json")
def parcours_path(eid, code="default"): return os.path.join(ELEVES_DIR, code, eid, "parcours.json")

def charger_parcours(eid, code="default"):
    p = parcours_path(eid, code)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    return {"sujets":{},"echanges":[],"xp":0,"badges":[],
            "maitrise":{},"lacunes":{},"ponts":[],
            "streak":0,"derniere_visite":"","defi_fait_aujourd_hui":False,
            "cartes_debloquees":[]}

def sauvegarder_parcours(eid, data, code="default"):
    os.makedirs(os.path.join(ELEVES_DIR, code, eid), exist_ok=True)
    with open(parcours_path(eid, code), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def charger_profil(eid, code="default"):
    p = profil_path(eid, code)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    return None

def mettre_a_jour_streak(eid, code="default"):
    data = charger_parcours(eid, code)
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
        sauvegarder_parcours(eid, data, code)
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
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return jsonify({
        "ok": True,
        "version": "Prof IA Web 1.0",
        "key_present": bool(key),
        "key_start": key[:8] if key else "VIDE",
        "env_keys": [k for k in os.environ.keys() if "ANTHROP" in k.upper() or "API" in k.upper()]
    })

@app.route("/api/eleves", methods=["GET"])
def lister_eleves():
    code = request.args.get("code", "default")
    ok, code = valider_code_famille(code)
    if not ok: return jsonify({"error": code}), 400
    _, code = get_famille_dir(code)
    dossier = os.path.join(ELEVES_DIR, code)
    eleves = []
    if not os.path.exists(dossier):
        return jsonify([])
    for d in sorted(os.listdir(dossier)):
        p = profil_path(d, code)
        if not os.path.exists(p): continue
        with open(p, "r", encoding="utf-8") as f: profil = json.load(f)
        parcours = charger_parcours(d, code)
        eleves.append({
            "id": d,
            "code": code,
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
    nom    = nettoyer_input(data.get("nom", "Élève"), 50)[:50] or "Élève"
    niveau = data.get("niveau", "adulte")
    aime   = nettoyer_input(data.get("aime", "tout"), 200)
    but    = nettoyer_input(data.get("but", "apprendre"), 200)
    code   = data.get("code", "default")
    ok, code = valider_code_famille(code)
    if not ok: return jsonify({"error": code}), 400
    _, code = get_famille_dir(code)
    eid    = f"{nom.lower().replace(' ','_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    os.makedirs(os.path.join(ELEVES_DIR, code, eid), exist_ok=True)
    profil = {"id": eid, "code": code, "nom": nom, "niveau": niveau,
              "aime": aime, "but": but, "positionne": False}
    with open(profil_path(eid, code), "w", encoding="utf-8") as f:
        json.dump(profil, f, ensure_ascii=False, indent=2)
    return jsonify({"id": eid, "code": code, "profil": profil})

@app.route("/api/eleves/<eid>", methods=["DELETE"])
def supprimer_eleve(eid):
    import shutil
    code = request.args.get("code", "default")
    _, code = get_famille_dir(code)
    dossier = os.path.join(ELEVES_DIR, code, eid)
    if os.path.exists(dossier):
        shutil.rmtree(dossier)
        return jsonify({"ok": True})
    return jsonify({"error": "Introuvable"}), 404

@app.route("/api/eleves/<eid>", methods=["GET"])
def get_eleve(eid):
    code = request.args.get("code", "default")
    _, code = get_famille_dir(code)
    profil = charger_profil(eid, code)
    if not profil: return jsonify({"error": "Élève introuvable"}), 404
    parcours = charger_parcours(eid, code)
    streak, est_nouveau = mettre_a_jour_streak(eid, code)
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
    code   = data.get("code", "default")
    _, code = get_famille_dir(code)
    msg = nettoyer_input(data.get("message", ""))
    if not eid or not msg:
        return jsonify({"error": "Paramètres manquants"}), 400
    if check_rate_limit(code):
        return jsonify({"error": "Limite de messages atteinte. Réessaie dans une heure."}), 429

    profil = charger_profil(eid, code)
    if not profil: return jsonify({"error": "Élève introuvable"}), 404

    parcours = charger_parcours(eid, code)
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
    sauvegarder_parcours(eid, parcours, code)

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
    code  = data.get("code", "default")
    _, code = get_famille_dir(code)
    score = data.get("score", 0)
    MAPPING = {0:"cp",1:"ce1",2:"ce2",3:"cm1",4:"cm2",
               5:"6e",6:"4e",7:"seconde",8:"premiere",9:"terminale",10:"adulte"}
    niveau = MAPPING.get(min(score,10), "6e")
    profil = charger_profil(eid, code)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    profil["niveau"] = niveau
    profil["positionne"] = True
    with open(profil_path(eid, code), "w", encoding="utf-8") as f:
        json.dump(profil, f, ensure_ascii=False, indent=2)
    return jsonify({"niveau": niveau, "profil": profil})

@app.route("/api/amorce", methods=["POST"])
def amorce():
    """Génère l'amorce proactive du prof au démarrage."""
    import anthropic
    data   = request.json
    eid    = data.get("eleve_id", "")
    code   = data.get("code", "default")
    _, code = get_famille_dir(code)
    profil = charger_profil(eid, code)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    parcours = charger_parcours(eid, code)
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
    code   = data.get("code", "default")
    _, code = get_famille_dir(code)
    profil = charger_profil(eid, code)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    parcours = charger_parcours(eid, code)
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
        code = data.get("code", "default")
        _, code = get_famille_dir(code)
        if result.get("correct"):
            parcours = charger_parcours(eid, code)
            parcours["xp"] = parcours.get("xp", 0) + 20
            parcours["defi_fait_aujourd_hui"] = True
            sauvegarder_parcours(eid, parcours, code)
            result["xp"] = parcours["xp"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Routes visualisation ──────────────────────────────────────

@app.route("/api/visualisation/<eid>", methods=["GET"])
def get_visualisation(eid):
    """Retourne toutes les données de visualisation d'un élève."""
    code = request.args.get("code", "default")
    _, code = get_famille_dir(code)
    profil = charger_profil(eid, code)
    if not profil: return jsonify({"error": "Introuvable"}), 404
    parcours = charger_parcours(eid, code)

    # Sujets par matière pour la forêt
    sujets_par_mat = parcours.get("sujets", {})
    if not isinstance(sujets_par_mat, dict): sujets_par_mat = {}

    # Ponts détectés pour les lianes
    ponts = parcours.get("ponts", [])

    # Maîtrise pour la taille des branches
    maitrise = parcours.get("maitrise", {})

    # Cartes débloquées
    cartes_ids = parcours.get("cartes_debloquees", [])

    # Construire les points géo à partir des sujets
    texte_sujets = " ".join(
        s for v in sujets_par_mat.values() for s in (v if isinstance(v,list) else [])
    ).lower()
    texte_maitrise = " ".join(maitrise.keys()).lower()
    texte_total = texte_sujets + " " + texte_maitrise

    # GEO_DB simplifié — mots-clés → coordonnées
    GEO = [
        {"lat":48.85,"lon":2.35,  "lb":"Paris",           "col":"#c9a84c","r":4, "mots":["paris","france","tour eiffel","eiffel"]},
        {"lat":30.05,"lon":31.23, "lb":"Le Caire",         "col":"#c9a84c","r":4, "mots":["caire","egypte","nil"]},
        {"lat":35.68,"lon":139.69,"lb":"Tokyo",            "col":"#c9a84c","r":3, "mots":["tokyo","japon","japonais"]},
        {"lat":51.5, "lon":-0.12, "lb":"Londres",          "col":"#c9a84c","r":3, "mots":["londres","angleterre","newton","shakespeare"]},
        {"lat":41.9, "lon":12.49, "lb":"Rome",             "col":"#c9a84c","r":3, "mots":["rome","romain","colisee","seneque"]},
        {"lat":37.97,"lon":23.72, "lb":"Athènes",          "col":"#c9a84c","r":3, "mots":["athenes","grece","socrate","platon","acropole"]},
        {"lat":52.52,"lon":13.4,  "lb":"Berlin",           "col":"#c9a84c","r":3, "mots":["berlin","allemagne","einstein","beethoven"]},
        {"lat":29.97,"lon":31.13, "lb":"Pyramides",        "col":"#1D9E75","r":5, "mots":["pyramide","gizeh","egypte","pharaon","imhotep"]},
        {"lat":48.86,"lon":2.29,  "lb":"Tour Eiffel",      "col":"#1D9E75","r":4, "mots":["eiffel","paris"]},
        {"lat":27.17,"lon":78.04, "lb":"Taj Mahal",        "col":"#1D9E75","r":4, "mots":["taj","inde","moghol"]},
        {"lat":41.89,"lon":12.49, "lb":"Colisée",          "col":"#1D9E75","r":4, "mots":["colisee","rome","gladiateur"]},
        {"lat":37.97,"lon":23.72, "lb":"Acropole",         "col":"#1D9E75","r":4, "mots":["acropole","athenes","parthenon"]},
        {"lat":47.58,"lon":-3.08, "lb":"Carnac",           "col":"#1D9E75","r":4, "mots":["carnac","megalithes","crowhurst","bretagne"]},
        {"lat":16.77,"lon":-3.0,  "lb":"Tombouctou",       "col":"#1D9E75","r":4, "mots":["tombouctou","mali","afrique","askia"]},
        {"lat":30.0, "lon":31.2,  "lb":"Égypte ancienne",  "col":"#534AB7","r":6, "mots":["egypte","pharaon","ptahhotep","ani","amenemope","imhotep","nefertiti"]},
        {"lat":37.5, "lon":23.5,  "lb":"Grèce antique",    "col":"#534AB7","r":5, "mots":["grece","socrate","platon","aristote","pythagore"]},
        {"lat":41.9, "lon":12.5,  "lb":"Empire romain",    "col":"#534AB7","r":5, "mots":["rome","romain","seneque","cesar","latin"]},
        {"lat":35.0, "lon":105.0, "lb":"Chine ancienne",   "col":"#534AB7","r":5, "mots":["chine","confucius","sun tzu","tao","lao tseu"]},
        {"lat":12.0, "lon":-8.0,  "lb":"Empire du Mali",   "col":"#534AB7","r":5, "mots":["mali","soundiata","tombouctou","afrique"]},
        {"lat":33.0, "lon":44.0,  "lb":"Mésopotamie",      "col":"#534AB7","r":4, "mots":["mesopotamie","babylone","sumer"]},
        {"lat":51.5, "lon":-0.12, "lb":"Newton",           "col":"#97C459","r":4, "mots":["newton","gravite","physique"]},
        {"lat":52.52,"lon":13.4,  "lb":"Einstein",         "col":"#97C459","r":4, "mots":["einstein","relativite"]},
        {"lat":48.85,"lon":2.35,  "lb":"Marie Curie",      "col":"#97C459","r":4, "mots":["curie","radioactivite","chimie"]},
        {"lat":51.5, "lon":-0.12, "lb":"Darwin",           "col":"#97C459","r":4, "mots":["darwin","evolution","especes","galapagos"]},
        {"lat":33.99,"lon":-6.85, "lb":"Ibn Battuta",      "col":"#D85A30","r":4, "mots":["ibn battuta","maroc","voyage"]},
        {"lat":40.42,"lon":-3.7,  "lb":"Colomb",           "col":"#D85A30","r":4, "mots":["colomb","amerique","espagne"]},
        {"lat":32.06,"lon":118.78,"lb":"Zheng He",         "col":"#D85A30","r":4, "mots":["zheng he","chine","flotte"]},
        {"lat":46.86,"lon":103.84,"lb":"Gengis Khan",      "col":"#D85A30","r":4, "mots":["gengis","khan","mongolie"]},
    ]

    points_actifs = []
    # Toujours Paris, Le Caire, Athènes comme base
    base = ["paris","caire","athenes"]
    for g in GEO:
        if any(m in texte_total for m in g["mots"]) or any(m in g["mots"] for m in base[:3] if m in g["mots"]):
            points_actifs.append({k:v for k,v in g.items() if k != "mots"})

    # Forêt — données par matière
    MATIERES = ["histoire","maths","sciences","geographie","français","philosophie","art",
                "zoologie","musique","langues","geologie","cuisine"]
    MATIERES_LABELS = {"histoire":"Histoire","maths":"Maths","sciences":"Sciences",
                       "geographie":"Géographie","français":"Français","philosophie":"Philo",
                       "art":"Art","zoologie":"Zoologie","musique":"Musique",
                       "langues":"Langues","geologie":"Géologie","cuisine":"Cuisine"}
    MATIERES_COLS = {"histoire":"#c9a84c","maths":"#534AB7","sciences":"#1D9E75",
                     "geographie":"#D85A30","français":"#97C459","philosophie":"#D4537E",
                     "art":"#5DCAA5","zoologie":"#EF9F27","musique":"#85B7EB",
                     "langues":"#E8A87C","geologie":"#A8A8A8","cuisine":"#FF6B35"}

    foret_data = []
    for mat in MATIERES:
        sujets = sujets_par_mat.get(mat, [])
        if isinstance(sujets, list) and len(sujets) > 0:
            score = sum(maitrise.get(s,0) for s in sujets)
            foret_data.append({
                "label": MATIERES_LABELS.get(mat, mat),
                "col": MATIERES_COLS.get(mat, "#c9a84c"),
                "nb_sujets": len(sujets),
                "sujets": sujets[:8],
                "score": score
            })

    # Si aucune matière explorée, montrer un arbre vide pour chaque
    if not foret_data:
        for mat in ["histoire","maths","sciences","geographie","français","philosophie","art"]:
            foret_data.append({
                "label": MATIERES_LABELS.get(mat, mat),
                "col": MATIERES_COLS.get(mat, "#c9a84c"),
                "nb_sujets": 0, "sujets": [], "score": 0
            })

    # Cartes débloquées avec leurs données complètes
    CARTES_MINI = {
        "socrate":   {"nom":"Socrate","epoque":"470–399 av. J.-C.","discipline":"Philosophie","rarity":"legendaire","citation":"Je sais que je ne sais rien.","stats":{"Sagesse":95,"Curiosité":99,"Impact":88,"Vision":90}},
        "newton":    {"nom":"Newton","epoque":"1643–1727","discipline":"Sciences","rarity":"legendaire","citation":"Si j'ai vu loin, c'est en me hissant sur des épaules de géants.","stats":{"Sagesse":80,"Curiosité":92,"Impact":99,"Vision":95}},
        "ptahhotep": {"nom":"Ptahhotep","epoque":"~2400 av. J.-C.","discipline":"Sagesse","rarity":"legendaire","citation":"Ne sois pas arrogant de ta connaissance.","stats":{"Sagesse":99,"Curiosité":88,"Impact":90,"Vision":96}},
        "curie":     {"nom":"Marie Curie","epoque":"1867–1934","discipline":"Sciences","rarity":"legendaire","citation":"Dans la vie, rien n'est à craindre, tout est à comprendre.","stats":{"Sagesse":85,"Curiosité":96,"Impact":97,"Vision":90}},
        "darwin":    {"nom":"Darwin","epoque":"1809–1882","discipline":"Sciences","rarity":"legendaire","citation":"Ce ne sont pas les espèces les plus fortes qui survivent, mais celles qui s'adaptent.","stats":{"Sagesse":82,"Curiosité":98,"Impact":98,"Vision":97}},
        "einstein":  {"nom":"Einstein","epoque":"1879–1955","discipline":"Sciences","rarity":"legendaire","citation":"L'imagination est plus importante que la connaissance.","stats":{"Sagesse":85,"Curiosité":99,"Impact":99,"Vision":98}},
        "ibnbattuta":{"nom":"Ibn Battuta","epoque":"1304–1368","discipline":"Géographie","rarity":"legendaire","citation":"Voyager, c'est triompher!","stats":{"Sagesse":85,"Curiosité":99,"Impact":88,"Vision":92}},
        "vinci":     {"nom":"Léonard de Vinci","epoque":"1452–1519","discipline":"Art","rarity":"legendaire","citation":"L'eau est le moteur de toute la nature.","stats":{"Sagesse":90,"Curiosité":99,"Impact":97,"Vision":99}},
        "gandhi":    {"nom":"Gandhi","epoque":"1869–1948","discipline":"Philosophie","rarity":"legendaire","citation":"Sois le changement que tu veux voir dans le monde.","stats":{"Sagesse":95,"Curiosité":82,"Impact":98,"Vision":96}},
        "confucius": {"nom":"Confucius","epoque":"551–479 av. J.-C.","discipline":"Philosophie","rarity":"legendaire","citation":"Peu importe la lenteur à laquelle tu vas, tant que tu ne t'arrêtes pas.","stats":{"Sagesse":96,"Curiosité":80,"Impact":95,"Vision":92}},
        "pieuvre":   {"nom":"La Pieuvre","epoque":"Vivante","discipline":"Zoologie","rarity":"rare","citation":"Trois coeurs, neuf cerveaux — une intelligence alien.","stats":{"Sagesse":70,"Curiosité":99,"Impact":80,"Vision":96}},
        "elephant":  {"nom":"L'Éléphant","epoque":"Vivant","discipline":"Zoologie","rarity":"rare","citation":"Les éléphants pleurent leurs morts et se souviennent de leurs amis.","stats":{"Sagesse":88,"Curiosité":85,"Impact":82,"Vision":86}},
        "bach":      {"nom":"Bach","epoque":"1685–1750","discipline":"Musique","rarity":"legendaire","citation":"La musique est une arithmétique de l'âme.","stats":{"Sagesse":90,"Curiosité":94,"Impact":99,"Vision":98}},
        "volcan":    {"nom":"Le Volcan","epoque":"Ancestral","discipline":"Géologie","rarity":"rare","citation":"Un volcan est le souffle de la Terre.","stats":{"Sagesse":80,"Curiosité":96,"Impact":96,"Vision":92}},
        "flocon":    {"nom":"Le Flocon","epoque":"Ancestral","discipline":"Géologie","rarity":"rare","citation":"Il n'existe pas deux flocons identiques — et pourtant tous hexagonaux.","stats":{"Sagesse":82,"Curiosité":98,"Impact":78,"Vision":96}},
    }

    cartes_debloquees = [
        {**CARTES_MINI[cid], "id": cid}
        for cid in cartes_ids if cid in CARTES_MINI
    ]

    # Aussi détecter nouvelles cartes selon sujets
    CARTES_MOTS = {
        "socrate":   ["socrate","philosophie","maieutique","grece"],
        "newton":    ["newton","gravite","physique","angleterre"],
        "ptahhotep": ["ptahhotep","egypte","sagesse","instructions"],
        "curie":     ["curie","radioactivite","chimie"],
        "darwin":    ["darwin","evolution","especes","galapagos","biologie"],
        "einstein":  ["einstein","relativite","physique"],
        "ibnbattuta":["ibn battuta","maroc","voyage","exploration"],
        "vinci":     ["vinci","peinture","renaissance","inventions"],
        "gandhi":    ["gandhi","inde","nonviolence"],
        "confucius": ["confucius","chine","sagesse","enseignement"],
        "pieuvre":   ["pieuvre","ocean","intelligence","cephalopode"],
        "elephant":  ["elephant","afrique","memoire","intelligence"],
        "bach":      ["bach","musique","gamme","baroque"],
        "volcan":    ["volcan","geologie","terre","magma"],
        "flocon":    ["flocon","neige","cristal","hexagone"],
    }

    nouvelles = []
    for cid, mots in CARTES_MOTS.items():
        if cid not in cartes_ids and any(m in texte_total for m in mots):
            cartes_ids.append(cid)
            if cid in CARTES_MINI:
                nouvelles.append({**CARTES_MINI[cid], "id": cid})
                cartes_debloquees.append({**CARTES_MINI[cid], "id": cid})

    if nouvelles:
        parc2 = charger_parcours(eid, code)
        parc2["cartes_debloquees"] = cartes_ids
        sauvegarder_parcours(eid, parc2, code)

    return jsonify({
        "foret": foret_data,
        "ponts": ponts,
        "globe_points": points_actifs,
        "cartes": cartes_debloquees,
        "nb_cartes_total": len(CARTES_MINI),
        "nouvelles_cartes": nouvelles,
        "xp": parcours.get("xp", 0),
        "nb_sujets_total": sum(len(v) for v in sujets_par_mat.values() if isinstance(v,list)),
    })

# ── Route Admin ──────────────────────────────────────────────

@app.route("/api/admin", methods=["GET"])
def admin():
    """Tableau de bord admin — protégé par code secret."""
    code_admin = request.args.get("code", "")
    code_secret = os.environ.get("ADMIN_CODE", "PROFIAADMIN")
    if code_admin != code_secret:
        return jsonify({"error": "Accès refusé"}), 403

    familles = []
    if not os.path.exists(ELEVES_DIR):
        return jsonify({"familles": []})

    for code_famille in sorted(os.listdir(ELEVES_DIR)):
        dossier = os.path.join(ELEVES_DIR, code_famille)
        if not os.path.isdir(dossier): continue
        eleves_data = []
        for eid in sorted(os.listdir(dossier)):
            p = profil_path(eid, code_famille)
            if not os.path.exists(p): continue
            with open(p, "r", encoding="utf-8") as f: profil = json.load(f)
            parcours = charger_parcours(eid, code_famille)
            nb_messages = len(parcours.get("echanges", []))
            sujets = []
            if isinstance(parcours.get("sujets"), dict):
                for v in parcours["sujets"].values(): sujets.extend(v)
            eleves_data.append({
                "nom": profil.get("nom", "?"),
                "niveau": profil.get("niveau", "adulte"),
                "xp": parcours.get("xp", 0),
                "streak": parcours.get("streak", 0),
                "nb_messages": nb_messages,
                "nb_sujets": len(sujets),
                "derniere_visite": parcours.get("derniere_visite", "jamais"),
                "badges": len(parcours.get("badges", [])),
                "cartes": len(parcours.get("cartes_debloquees", [])),
            })
        if eleves_data:
            familles.append({
                "code": code_famille,
                "nb_eleves": len(eleves_data),
                "eleves": eleves_data,
                "total_messages": sum(e["nb_messages"] for e in eleves_data),
            })

    return jsonify({
        "nb_familles": len(familles),
        "familles": familles
    })

# ── Sert le frontend ──────────────────────────────────────────

@app.route("/")
def home():
    return send_from_directory(app.static_folder, "accueil.html")

@app.route("/<path:path>")
def serve_static(path):
    filepath = os.path.join(app.static_folder, path)
    if os.path.exists(filepath):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")

@app.after_request
def ajouter_headers(response):
    """Headers de sécurité sur toutes les réponses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Prof IA backend — http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

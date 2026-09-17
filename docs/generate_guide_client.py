"""Guide de test pour le client (PDF) : fonctionnalités de l'app et scénarios de test.

Usage : python generate_guide_client.py  (crée Guide_test_client.pdf à côté)
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).parent / "Guide_test_client.pdf"
VERSION = "version de test du 17/09/2026"

# ----- Palette (identique au README client) -----
HEX = {
    "ink": "#0F172A", "ink_soft": "#334155", "ink_mute": "#64748B", "border": "#E2E8F0",
    "bg_soft": "#F8FAFC", "bg_accent": "#EEF2FF", "primary": "#4F46E5", "success": "#059669",
    "warning": "#B45309", "bg_warning": "#FEF3C7", "bg_success": "#ECFDF5",
}
C = {k: colors.HexColor(v) for k, v in HEX.items()}
W = A4[0] - 4 * cm


def style(name: str, **kw) -> ParagraphStyle:
    base = dict(fontName="Helvetica", fontSize=10.5, leading=15, textColor=C["ink_soft"],
                spaceAfter=6, alignment=TA_LEFT)
    base.update(kw)
    return ParagraphStyle(name, **base)


BODY = style("body")
SMALL = style("small", fontSize=9, leading=12.5, textColor=C["ink_mute"])
H1 = style("h1", fontName="Helvetica-Bold", fontSize=25, leading=30, textColor=C["ink"], spaceAfter=4)
# keepWithNext : un titre ne reste jamais seul en bas de page.
H2 = style("h2", fontName="Helvetica-Bold", fontSize=15, leading=20, textColor=C["ink"], spaceBefore=16, spaceAfter=8, keepWithNext=1)
H3 = style("h3", fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=C["primary"], spaceBefore=10, spaceAfter=4, keepWithNext=1)
BULLET = style("bullet", leftIndent=14, bulletIndent=2, spaceAfter=3)
CELL = style("cell", fontSize=9, leading=12.2, spaceAfter=0)
CELL_B = style("cell_b", fontName="Helvetica-Bold", fontSize=9, leading=12.2, textColor=C["ink"], spaceAfter=0)
HEAD = style("head", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=colors.white, spaceAfter=0)


def nb(text: str) -> str:
    """Espaces insécables autour des guillemets français : « et » ne se détachent plus du mot."""
    return text.replace("« ", "« ").replace(" »", " »")


def bullets(items: list[str]) -> list[Paragraph]:
    return [Paragraph(nb(text), BULLET, bulletText="•") for text in items]


def callout(title: str, lines: list[str], tone: str = "info") -> Table:
    bg, fg = {
        "info": ("bg_accent", "primary"),
        "warning": ("bg_warning", "warning"),
        "success": ("bg_success", "success"),
    }[tone]
    content = [Paragraph(f'<font color="{HEX[fg]}"><b>{title}</b></font>', style("ct", spaceAfter=4))]
    content += [Paragraph(nb(line), style("cl", fontSize=10, leading=14, spaceAfter=3)) for line in lines]
    table = Table([[content]], colWidths=[W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C[bg]),
        ("LINEBEFORE", (0, 0), (0, -1), 3, C[fg]),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return table


def grid(header: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(h, HEAD) for h in header]]
    data += [[Paragraph(nb(cell), CELL_B if i == 0 else CELL) for i, cell in enumerate(row)] for row in rows]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), C["ink"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, C["border"]),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, C["bg_soft"]]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(C["ink_mute"])
    canvas.drawString(2 * cm, 1.2 * cm, f"Deriv Trading Bot · Guide de test · {VERSION}")
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build() -> None:
    s: list = []

    # ------------------------------------------------------------------ Couverture
    s += [
        Paragraph("Deriv Trading Bot", H1),
        Paragraph("Guide de présentation et de test de l'application Android", style("sub", fontSize=13, leading=18, textColor=C["ink_mute"], spaceAfter=14)),
        callout("En bref", [
            "Une application Android qui pilote un <b>robot de trading</b> sur les indices synthétiques Deriv, "
            "diffuse des <b>signaux de trading en direct</b> avec notifications, et intègre (bientôt) le <b>copy trading</b>.",
            "Ce document présente les fonctionnalités, l'installation et <b>12 scénarios de test</b> à dérouler. "
            "Merci de nous remonter tout ce qui ne fonctionne pas comme décrit.",
        ]),
        Spacer(1, 10),
        callout("Important avant de tester", [
            "<b>Testez uniquement avec un compte DÉMO Deriv</b> (argent virtuel). Aucun test ne doit être fait avec de l'argent réel pendant cette phase.",
            "Les indices synthétiques Deriv sont produits par un générateur aléatoire : <b>aucun robot ni signal ne garantit un gain</b>. "
            "Sur Deriv, un trade gagnant rapporte environ 0,88 $ pour 1 $ misé et un trade perdant coûte 1 $ : sur la durée, le résultat moyen est négatif. "
            "Les garde-fous (stop loss et take profit du jour) servent à limiter les pertes.",
            "Ne communiquez jamais votre mot de passe ni votre token Deriv, y compris à notre équipe.",
        ], tone="warning"),
    ]

    # ------------------------------------------------------------------ Fonctionnalités
    s.append(Paragraph("1. Fonctionnalités", H2))

    s.append(Paragraph("Compte et abonnement", H3))
    s += bullets([
        "Création de compte par e-mail et mot de passe (8 caractères minimum), connexion sécurisée.",
        "<b>Essai gratuit de 7 jours</b> dès l'inscription : accès au compte réel et aux signaux en direct. Le nombre de jours restants s'affiche sur l'accueil.",
        "<b>Premium</b> : 6 000 FCFA par mois ou 55 000 FCFA par an (2 mois offerts), paiement par Mobile Money. <i>Paiement en cours d'activation.</i>",
        "Bouton « Créer un compte Deriv » pour ouvrir un compte chez Deriv depuis l'application.",
    ])

    s.append(Paragraph("Robot de trading", H3))
    s += bullets([
        "<b>3 stratégies</b> : <b>Rise / Fall</b> (direction du prochain mouvement), <b>Over / Under</b> (dernier chiffre du prix), "
        "<b>Martingale</b> (Rise / Fall avec mise doublée après une perte, plafonnée).",
        "<b>7 indices</b> : Volatility 10, 25, 50, 75 et 100, Boom 500, Crash 500.",
        "Réglages : mise par trade, <b>stop loss du jour</b> et <b>take profit du jour</b>.",
        "Choix du compte <b>Démo</b> ou <b>Réel</b> (le compte réel exige l'essai gratuit ou le Premium).",
        "Suivi en temps réel : état du robot, gain ou perte de la session, solde, trades gagnés et perdus, derniers trades.",
        "<b>Arrêt automatique</b> au stop loss ou au take profit. Le robot s'arrête <b>avant</b> qu'une mise puisse faire dépasser le stop loss, y compris en Martingale.",
        "Le robot tourne sur le serveur : il continue de trader même si l'application est fermée.",
    ])

    s.append(Paragraph("Signaux de trading", H3))
    s += bullets([
        "Analyse en continu de <b>6 indices</b> : Volatility 75, Volatility 100, Boom 1000, Crash 1000, Boom 500, Crash 500.",
        "<b>3 stratégies</b> : croisement de moyennes mobiles, RSI, détection de spike (Boom et Crash uniquement).",
        "Chaque signal indique la direction (achat ou vente), le prix d'entrée, le stop loss et le take profit ; il expire après 15 minutes.",
        "Chaque signal est suivi automatiquement (TP atteint, SL touché ou expiré) : l'application affiche la <b>performance réelle</b> sur 7 jours, globale et par stratégie.",
        "<b>Mes signaux</b> : chaque utilisateur choisit ses indices, ses stratégies et active ou coupe les notifications.",
        "Signaux en direct et notifications réservés à l'essai gratuit et au Premium ; sans abonnement, seul l'historique est visible.",
        "Notifications reçues quand l'application est ouverte ou en arrière-plan.",
    ])

    s.append(Paragraph("Copy trading (bientôt disponible)", H3))
    s += bullets([
        "Copie automatique des trades d'un trader expérimenté désigné par l'administrateur, avec une mise adaptée au capital de chaque suiveur "
        "et un stop loss journalier par suiveur.",
        "Fonction développée et testée, <b>activée après validation</b> : l'écran affiche pour l'instant « Bientôt disponible ».",
    ])

    s.append(Paragraph("Console d'administration (web)", H3))
    s += bullets([
        "Statistiques : utilisateurs, essais en cours, abonnés Premium, chiffre d'affaires.",
        "Gestion des utilisateurs : recherche, suspension, suppression, offrir ou retirer du Premium, relancer l'essai, réinitialiser un mot de passe, historique des paiements.",
        "Adresse : https://api1.innovahub226.com/admin (accès administrateur communiqué séparément).",
    ])

    s.append(Paragraph("Sécurité", H3))
    s += bullets([
        "Chaque utilisateur ne voit et ne pilote que son propre robot.",
        "Le token Deriv est conservé chiffré sur le téléphone et transmis au serveur uniquement pour démarrer le robot. "
        "Avec les autorisations « Read » et « Trade », il ne permet aucun retrait d'argent.",
        "Protection contre les tentatives répétées de connexion.",
    ])

    # ------------------------------------------------------------------ Installation
    s.append(Paragraph("2. Installation et préparation", H2))
    s.append(Paragraph("Installer l'application", H3))
    s += bullets([
        "Récupérez le fichier APK transmis (Deriv Trading Bot, environ 49 Mo) et ouvrez-le sur le téléphone Android.",
        "Si Android le demande, autorisez l'installation d'applications depuis cette source.",
        "Ouvrez l'application. Lorsque Android le propose, <b>autorisez les notifications</b>.",
    ])
    s.append(Paragraph("Application testée sur Samsung Galaxy A15 (Android 16).", SMALL))
    s.append(Paragraph("Préparer un token Deriv démo", H3))
    s += bullets([
        "Connectez-vous à votre espace Deriv (ou créez un compte avec le bouton « Créer un compte Deriv » de l'application).",
        "Dans la section API, créez un jeton d'accès avec <b>uniquement</b> les autorisations <b>Read</b> et <b>Trade</b>. N'activez jamais « Payments ».",
        "Vérifiez que votre compte dispose d'un <b>compte démo</b> : c'est lui que le robot utilisera quand l'application est réglée sur « Démo ».",
    ])

    # ------------------------------------------------------------------ Scénarios
    s.append(Paragraph("3. Scénarios de test", H2))
    s.append(Paragraph(nb("Pour chaque scénario, notez « OK » ou décrivez le problème rencontré (voir la section 5)."), BODY))
    widths = [1.0 * cm, 3.4 * cm, 7.0 * cm, W - 11.4 * cm]
    rows = [
        ["T1", "Inscription et essai", "Ouvrir l'application, « Créer un compte », saisir e-mail et mot de passe.", "Accueil connecté avec le bandeau « Essai gratuit — 7 jours restants »."],
        ["T2", "Token Deriv", "Écran « Connexion Deriv » : coller le token démo, « Enregistrer &amp; connecter ».", "Retour à l'accueil, sans message d'erreur."],
        ["T3", "Réglages du robot", "Accueil, « Modifier » : choisir un indice, la stratégie Rise / Fall, mise 1, stop loss 10, take profit 20, puis appliquer.", "Les valeurs choisies s'affichent sur l'accueil."],
        ["T4", "Robot en démo", "Vérifier que « Démo » est sélectionné, « Démarrer le robot », attendre 2 à 3 minutes, puis « Arrêter le robot ».", "État « EN MARCHE », en-tête « Compte démo », trades qui apparaissent, gain ou perte et solde mis à jour ; puis état « ARRÊTÉ »."],
        ["T5", "Garde-fous", "Relancer le robot en démo et le laisser tourner jusqu'à l'arrêt automatique.", "Arrêt seul au take profit ou au stop loss ; la perte ne dépasse jamais le stop loss réglé."],
        ["T6", "Martingale", "Réglages : stratégie Martingale, mise 1, stop loss 10. Démarrer en démo.", "La mise double après une perte, revient à la mise de départ après un gain ; arrêt avant de dépasser le stop loss."],
        ["T7", "Double démarrage", "Pendant que le robot tourne, essayer de le démarrer une seconde fois.", "Démarrage refusé : une seule session à la fois par utilisateur."],
        ["T8", "Signaux en direct", "Accueil, carte « Signaux ».", "Badge « EN DIRECT », avertissement, performance réelle sur 7 jours, liste des signaux et filtres."],
        ["T9", "Mes signaux", "Écran Signaux, bouton « Mes signaux » : décocher des indices ou des stratégies, puis « Enregistrer ».", "Message « Préférences enregistrées » ; le résumé affiché reflète vos choix."],
        ["T10", "Notifications", "Laisser l'application en arrière-plan quelques minutes, notifications activées.", "Une notification « ACHAT » ou « VENTE » arrive pour un signal correspondant à vos choix."],
        ["T11", "Token refusé", "« Changer de token », coller un token volontairement erroné, puis « Démarrer le robot ».", "Message « Deriv refuse votre token API » avec un bouton « Changer »."],
        ["T12", "Offres Premium", "Accueil, bouton du bandeau (« Voir les formules » ou « Passer au premium »).", "Affichage des offres mensuelle et annuelle. Le paiement n'est pas encore actif."],
    ]
    s.append(grid(["#", "Scénario", "Étapes", "Résultat attendu"], rows, widths))

    # ------------------------------------------------------------------ Limites
    s.append(Paragraph("4. Pas encore actif dans cette version", H2))
    s += bullets([
        "<b>Paiement Mobile Money</b> du Premium : en attente de la configuration du compte marchand.",
        "<b>Lien d'affiliation Deriv</b> : le bouton ouvre l'inscription Deriv, sans le code partenaire pour l'instant.",
        "<b>Notifications application complètement fermée</b> : prévues dans une prochaine version.",
        "<b>Copy trading</b> : désactivé en attente de validation.",
        "<b>Compte réel</b> : fonctionnel, mais à ne pas tester pendant cette phase.",
    ])

    # ------------------------------------------------------------------ Retours
    s.append(KeepTogether([
        Paragraph("5. Comment nous remonter un problème", H2),
        callout("Pour chaque problème, merci d'indiquer", [
            "1. Le <b>numéro du scénario</b> (par exemple T4).",
            "2. Le <b>modèle du téléphone</b> et la <b>version d'Android</b>.",
            "3. Ce que vous avez fait, ce qui était attendu et ce qui s'est passé.",
            "4. Une <b>capture d'écran</b> et l'<b>heure approximative</b> : elle nous permet de retrouver l'événement dans les journaux du serveur.",
        ], tone="success"),
    ]))

    doc = SimpleDocTemplate(
        str(OUT), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=2 * cm,
        title="Deriv Trading Bot - Guide de test", author="Deriv Trading Bot",
    )
    doc.build(s, onFirstPage=footer, onLaterPages=footer)
    print(f"PDF genere : {OUT} ({OUT.stat().st_size // 1024} Ko)")


if __name__ == "__main__":
    build()

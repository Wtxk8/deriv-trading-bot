"""Génère un README.pdf pour le client — mise en page propre via reportlab.

Usage : python generate_pdf.py  (crée README.pdf à côté)
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = Path(__file__).parent / "README.pdf"

# ----- Palette (accord avec l'app dark) sur fond blanc pour lisibilité print -----
INK = colors.HexColor("#0F172A")
INK_SOFT = colors.HexColor("#334155")
INK_MUTE = colors.HexColor("#64748B")
BORDER = colors.HexColor("#E2E8F0")
BG_SOFT = colors.HexColor("#F8FAFC")
BG_ACCENT = colors.HexColor("#EEF2FF")
PRIMARY = colors.HexColor("#4F46E5")
SUCCESS = colors.HexColor("#059669")
DANGER = colors.HexColor("#DC2626")
WARNING = colors.HexColor("#B45309")

styles = getSampleStyleSheet()

BODY = ParagraphStyle(
    "body",
    parent=styles["Normal"],
    fontName="Helvetica",
    fontSize=10.5,
    leading=15,
    textColor=INK_SOFT,
    alignment=TA_JUSTIFY,
    spaceAfter=8,
)
H1 = ParagraphStyle(
    "h1",
    parent=styles["Heading1"],
    fontName="Helvetica-Bold",
    fontSize=22,
    leading=26,
    textColor=INK,
    spaceBefore=6,
    spaceAfter=6,
    alignment=TA_LEFT,
)
H2 = ParagraphStyle(
    "h2",
    parent=styles["Heading2"],
    fontName="Helvetica-Bold",
    fontSize=14,
    leading=18,
    textColor=INK,
    spaceBefore=18,
    spaceAfter=8,
)
H3 = ParagraphStyle(
    "h3",
    parent=styles["Heading3"],
    fontName="Helvetica-Bold",
    fontSize=11.5,
    leading=15,
    textColor=PRIMARY,
    spaceBefore=10,
    spaceAfter=4,
)
CAPTION = ParagraphStyle(
    "caption",
    parent=BODY,
    fontName="Helvetica",
    fontSize=9,
    leading=12,
    textColor=INK_MUTE,
    spaceAfter=6,
)
CODE = ParagraphStyle(
    "code",
    parent=BODY,
    fontName="Courier",
    fontSize=9.5,
    leading=13,
    textColor=INK,
    backColor=BG_SOFT,
    borderColor=BORDER,
    borderWidth=0.5,
    borderPadding=8,
    leftIndent=0,
    spaceAfter=10,
    alignment=TA_LEFT,
)
BULLET = ParagraphStyle(
    "bullet",
    parent=BODY,
    leftIndent=14,
    bulletIndent=0,
    spaceAfter=4,
)


class HR(Flowable):
    def __init__(self, width: float, thickness: float = 0.5, color=BORDER) -> None:
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.color = color

    def draw(self) -> None:  # noqa: D401
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.width, 0)

    def wrap(self, availW, availH):
        return self.width, self.thickness


def callout(text: str, tone: str = "info", inner_width: float = 16 * cm) -> Table:
    palette = {
        "info": (BG_ACCENT, PRIMARY),
        "success": (colors.HexColor("#ECFDF5"), SUCCESS),
        "warning": (colors.HexColor("#FEF3C7"), WARNING),
        "danger": (colors.HexColor("#FEE2E2"), DANGER),
    }
    bg, accent = palette.get(tone, palette["info"])
    para = Paragraph(text, ParagraphStyle("callout", parent=BODY, textColor=INK, spaceAfter=0))
    tbl = Table([[para]], colWidths=[inner_width])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("LINEBEFORE", (0, 0), (-1, -1), 3, accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return tbl


def kv_table(rows: list[tuple[str, str]], col_w=(4.5 * cm, 11.5 * cm)) -> Table:
    data = []
    for k, v in rows:
        data.append(
            [
                Paragraph(k, ParagraphStyle("k", parent=BODY, fontName="Helvetica-Bold", textColor=INK, alignment=TA_LEFT, spaceAfter=0)),
                Paragraph(v, ParagraphStyle("v", parent=BODY, spaceAfter=0)),
            ]
        )
    tbl = Table(data, colWidths=col_w)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BG_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return tbl


def endpoints_table(rows: list[tuple[str, str, str]]) -> Table:
    header = [
        Paragraph("<b>Méthode</b>", BODY),
        Paragraph("<b>Chemin</b>", BODY),
        Paragraph("<b>Description</b>", BODY),
    ]
    data = [header]
    for m, p, d in rows:
        data.append(
            [
                Paragraph(f'<font face="Courier">{m}</font>', BODY),
                Paragraph(f'<font face="Courier">{p}</font>', BODY),
                Paragraph(d, BODY),
            ]
        )
    tbl = Table(data, colWidths=(2.4 * cm, 6.5 * cm, 7.1 * cm))
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BG_ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return tbl


def _page_number(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(INK_MUTE)
    canvas.drawRightString(A4[0] - 2 * cm, 1.4 * cm, f"Page {doc.page}")
    canvas.setFillColor(BORDER)
    canvas.rect(2 * cm, 1.85 * cm, A4[0] - 4 * cm, 0.4, fill=1, stroke=0)
    canvas.setFillColor(INK_MUTE)
    canvas.drawString(2 * cm, 1.4 * cm, "Deriv Trading Bot — Guide client")
    canvas.restoreState()


def cover_flowables(width: float) -> list:
    cover_bg = Table(
        [
            [
                Paragraph(
                    '<font size=8 color="#4F46E5"><b>DOCUMENTATION CLIENT</b></font><br/><br/>'
                    '<font size=30 color="#0F172A"><b>Deriv Trading Bot</b></font><br/><br/>'
                    '<font size=13 color="#334155">Robot de trading pour indices synthétiques Deriv, '
                    "piloté depuis une application mobile Android et une console web d'administration.</font>",
                    BODY,
                )
            ]
        ],
        colWidths=[width],
    )
    cover_bg.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BG_SOFT),
                ("BOX", (0, 0), (-1, -1), 0, BG_SOFT),
                ("LEFTPADDING", (0, 0), (-1, -1), 32),
                ("RIGHTPADDING", (0, 0), (-1, -1), 32),
                ("TOPPADDING", (0, 0), (-1, -1), 40),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 40),
            ]
        )
    )

    meta = kv_table(
        [
            ("Version du document", "1.0 — 21 août 2026"),
            ("Livrables", "Backend FastAPI (Docker), APK Android, console web admin"),
            ("URL de l'API", '<font face="Courier">https://api1.innovahub226.com</font>'),
            ("URL Admin", '<font face="Courier">https://api1.innovahub226.com/admin</font>'),
            ("Dépôt code source", '<font face="Courier">github.com/Wtxk8/deriv-trading-bot</font>'),
        ],
        col_w=(5 * cm, 11 * cm),
    )

    return [
        Spacer(1, 1 * cm),
        cover_bg,
        Spacer(1, 1 * cm),
        meta,
        Spacer(1, 0.8 * cm),
        callout(
            "<b>Ce document</b> décrit ce que le projet fait, comment il est structuré, "
            "comment l'utiliser au quotidien et comment le maintenir. Il est destiné au commanditaire "
            "du projet et à ses équipes techniques.",
            tone="info",
            inner_width=width,
        ),
    ]


def content_flowables(width: float) -> list:
    st = []

    # 1 — Vue d'ensemble
    st.append(Paragraph("1. Vue d'ensemble", H2))
    st.append(
        Paragraph(
            "Le projet fournit un robot de trading (« bot ») qui exécute des ordres sur les indices "
            "synthétiques Deriv (R_10, R_25, R_50, R_100). Le bot tourne <b>côté serveur</b> en continu ; "
            "l'application mobile et la console web sont des <b>télécommandes</b> qui contrôlent son état "
            "et affichent le solde et les résultats en temps réel.",
            BODY,
        )
    )
    st.append(HR(width))

    st.append(Paragraph("2. Architecture", H2))
    st.append(
        Paragraph(
            "Trois composants principaux :",
            BODY,
        )
    )
    st.append(
        Paragraph(
            "• <b>Backend FastAPI</b> (Python, conteneurisé Docker) — moteur du bot, gestion des utilisateurs "
            "et des rôles, exposition d'une API REST et d'un flux WebSocket.",
            BULLET,
        )
    )
    st.append(
        Paragraph(
            "• <b>Application mobile Android</b> (Flutter) — pilotage à distance, saisie du token API Deriv, "
            "consultation du PnL en temps réel, historique des trades.",
            BULLET,
        )
    )
    st.append(
        Paragraph(
            "• <b>Console web d'administration</b> (HTML/JS servie par le backend) — gestion des comptes "
            "utilisateurs, rôles et statuts.",
            BULLET,
        )
    )
    st.append(Spacer(1, 4))
    st.append(
        callout(
            "<b>Communication avec Deriv.</b> Le bot dialogue avec la nouvelle API Deriv (2026) via "
            "un token PAT (Personal Access Token) et un flux OTP + WebSocket pré-authentifié. "
            "Les tokens PAT sont fournis par l'utilisateur final ; ils ne sont jamais transmis à des tiers.",
            tone="info",
            inner_width=width,
        )
    )

    st.append(Paragraph("3. Fonctionnalités livrées", H2))

    st.append(Paragraph("3.1 Application mobile", H3))
    st.append(
        Paragraph(
            "• Écran de connexion (compte applicatif) ou de saisie directe du token API Deriv.<br/>"
            "• Tableau de bord : statut du robot (EN MARCHE / EN PAUSE / STOP LOSS / TAKE PROFIT), "
            "PnL de session, solde, taux de réussite, garde-fous jour (SL/TP).<br/>"
            "• Carte stratégie active + configuration en bottom-sheet (indice, mise, SL, TP, stratégie).<br/>"
            "• Liste des derniers trades avec direction, mise et résultat.<br/>"
            "• Bouton unique <b>Démarrer / Arrêter le robot</b>.<br/>"
            "• Console admin embarquée (visible uniquement aux comptes avec rôle <font face=\"Courier\">admin</font>).",
            BODY,
        )
    )

    st.append(Paragraph("3.2 Console web admin", H3))
    st.append(
        Paragraph(
            "• Authentification par email + mot de passe (accès réservé aux comptes admin).<br/>"
            "• Liste des utilisateurs avec statistiques (total, admins, actifs, suspendus).<br/>"
            "• Recherche par nom ou email.<br/>"
            "• Actions par utilisateur : promouvoir / rétrograder, suspendre / réactiver, supprimer.<br/>"
            "• Boîte de dialogue de confirmation systématique avant toute modification.",
            BODY,
        )
    )

    st.append(Paragraph("3.3 Sécurité & confidentialité", H3))
    st.append(
        Paragraph(
            "• Mots de passe stockés hachés (bcrypt).<br/>"
            "• Sessions par JWT signé (HS256), expiration 12 h par défaut.<br/>"
            "• Token API Deriv chiffré localement sur le téléphone (Keystore Android).<br/>"
            "• API exposée via tunnel Cloudflare (HTTPS).<br/>"
            "• Un administrateur ne peut ni se rétrograder ni se supprimer lui-même.",
            BODY,
        )
    )

    st.append(PageBreak())
    st.append(Paragraph("4. Utilisation au quotidien", H2))

    st.append(Paragraph("4.1 Connexion mobile", H3))
    st.append(
        Paragraph(
            "1. Installer l'APK sur un appareil Android (via <font face=\"Courier\">adb install</font> ou "
            "en transférant le fichier).<br/>"
            "2. Au premier lancement, saisir un <b>token API Deriv</b> obtenu depuis l'espace Deriv "
            "(<font face=\"Courier\">home.deriv.com → API tokens</font>) avec les droits Read + Trade.<br/>"
            "3. Sauvegarder — le tableau de bord s'ouvre avec le solde du compte démo Deriv.<br/>"
            "4. Régler la stratégie et les garde-fous, puis <b>Démarrer le robot</b>.",
            BODY,
        )
    )
    st.append(
        callout(
            "<b>Recommandation.</b> Tester uniquement en <b>compte démo Deriv</b> (10 000 USD virtuels) "
            "avant d'envisager un usage réel. Les stratégies fournies (Rise/Fall, Over/Under) sont "
            "à but de démonstration ; elles ne garantissent aucun profit.",
            tone="warning",
            inner_width=width,
        )
    )

    st.append(Paragraph("4.2 Console web admin", H3))
    st.append(
        Paragraph(
            f'URL : <font face="Courier" color="#4F46E5">https://api1.innovahub226.com/admin</font>',
            BODY,
        )
    )
    st.append(
        Paragraph(
            "Se connecter avec un compte admin. La liste des utilisateurs s'affiche avec les actions "
            "disponibles. Toute modification prend effet immédiatement côté serveur — l'utilisateur "
            "concerné voit son accès ajusté à sa prochaine action.",
            BODY,
        )
    )

    st.append(Paragraph("4.3 Cycle d'un trade", H3))
    st.append(
        Paragraph(
            "Une fois démarré, le robot écoute les ticks de l'indice choisi, applique la stratégie pour "
            "décider d'un contrat (montée/baisse ou over/under), l'achète, puis attend son règlement. "
            "Le PnL de session s'incrémente à chaque trade. Dès que <b>Stop Loss</b> ou <b>Take Profit</b> "
            "journalier est atteint, le robot s'arrête automatiquement — aucune intervention manuelle "
            "requise pour couper.",
            BODY,
        )
    )

    st.append(HR(width))
    st.append(Paragraph("5. Endpoints API principaux", H2))
    st.append(
        Paragraph(
            'Base URL : <font face="Courier">https://api1.innovahub226.com</font>. '
            "Les endpoints marqués <i>auth</i> exigent un JWT dans l'en-tête <font face=\"Courier\">"
            "Authorization: Bearer &lt;token&gt;</font>. Les endpoints <i>admin</i> exigent en plus "
            'que le token porte le rôle <font face="Courier">admin</font>.',
            BODY,
        )
    )
    st.append(
        endpoints_table(
            [
                ("POST", "/register", "Créer un compte (rôle user par défaut)."),
                ("POST", "/login", "Émettre un JWT après vérif email/mot de passe."),
                ("GET", "/me", "Profil courant (auth)."),
                ("GET", "/admin/users", "Liste des utilisateurs (admin)."),
                ("PATCH", "/admin/users/{id}", "Rôle ou statut d'un utilisateur (admin)."),
                ("DELETE", "/admin/users/{id}", "Suppression d'un compte (admin)."),
                ("POST", "/api/bot/start", "Démarrer le bot avec un token PAT Deriv."),
                ("POST", "/api/bot/stop", "Arrêter le bot immédiatement."),
                ("GET", "/api/bot/status", "État courant + PnL + 5 derniers trades."),
                ("WS", "/ws/bot/status", "Flux temps réel du même état (push ~1s)."),
                ("GET", "/health", "Sonde de vie (200 OK)."),
            ]
        )
    )

    st.append(PageBreak())
    st.append(Paragraph("6. Déploiement & maintenance", H2))

    st.append(Paragraph("6.1 Serveur", H3))
    st.append(
        Paragraph(
            "Le backend est distribué en image Docker (base <font face=\"Courier\">python:3.11-slim</font>) "
            "avec fichier <font face=\"Courier\">docker-compose.yml</font>. Les données (comptes, JWT secret, "
            "config) sont dans un volume nommé qui survit aux rebuilds.",
            BODY,
        )
    )
    st.append(Paragraph("Redéploiement à partir de zéro :", BODY))
    st.append(
        Paragraph(
            "git clone https://github.com/Wtxk8/deriv-trading-bot.git<br/>"
            "cd deriv-trading-bot/backend<br/>"
            "# éditer .env (JWT_SECRET, DEFAULT_ADMIN_PASSWORD, DERIV_APP_ID)<br/>"
            "docker compose up -d --build",
            CODE,
        )
    )
    st.append(Paragraph("Mise à jour continue :", BODY))
    st.append(
        Paragraph(
            "git pull<br/>"
            "docker compose up -d --build",
            CODE,
        )
    )

    st.append(Paragraph("6.2 Variables d'environnement clés", H3))
    st.append(
        kv_table(
            [
                ("JWT_SECRET", "Clé HS256 pour signer les JWT. 64 caractères aléatoires recommandés."),
                ("JWT_EXPIRES_MINUTES", "Durée de validité d'un JWT (720 = 12 h par défaut)."),
                ("DEFAULT_ADMIN_EMAIL", "Email du compte admin créé au premier démarrage."),
                ("DEFAULT_ADMIN_PASSWORD", "Mot de passe initial de ce compte (à changer)."),
                ("DERIV_APP_ID", "Deriv-App-ID enregistré sur api.deriv.com pour l'API PAT."),
                ("TRADING_DATABASE_URL", "URL base de données. SQLite par défaut."),
            ],
            col_w=(4.8 * cm, 11.2 * cm),
        )
    )

    st.append(Paragraph("6.3 Application mobile", H3))
    st.append(
        Paragraph(
            "L'APK signée en mode release est produite avec :",
            BODY,
        )
    )
    st.append(
        Paragraph(
            "cd mobile<br/>"
            "flutter build apk --release<br/>"
            "# APK : build/app/outputs/flutter-apk/app-release.apk",
            CODE,
        )
    )
    st.append(
        Paragraph(
            "Pour installer sur un téléphone connecté via USB en mode développeur :",
            BODY,
        )
    )
    st.append(Paragraph("adb install -r app-release.apk", CODE))

    st.append(Paragraph("7. Support & évolution", H2))
    st.append(
        Paragraph(
            "Le code source complet est hébergé sur GitHub. Chaque évolution passe par un commit / push "
            "puis un rebuild Docker côté serveur (30 s à 2 min selon les changements). L'application "
            "mobile est mise à jour en régénérant une APK signée et en l'installant.",
            BODY,
        )
    )
    st.append(
        callout(
            "<b>Limites connues.</b> (1) Le bot exécute des stratégies simples à but de démonstration technique — "
            "aucune promesse de rentabilité. (2) La console web admin ne permet pas encore la création de "
            "compte : les nouveaux utilisateurs s'enregistrent via l'endpoint <font face=\"Courier\">"
            "POST /register</font>. (3) Les stratégies avancées (Martingale, pause après pertes, alertes push) "
            "sont maquettées dans l'UI mais non branchées côté moteur.",
            tone="warning",
            inner_width=width,
        )
    )

    st.append(HR(width))
    st.append(
        Paragraph(
            'Document généré automatiquement à partir de <font face="Courier">docs/generate_pdf.py</font>. '
            "Contact technique via le dépôt GitHub du projet.",
            CAPTION,
        )
    )
    return st


def build() -> None:
    OUT.parent.mkdir(exist_ok=True, parents=True)
    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2.4 * cm,
        title="Deriv Trading Bot — Guide client",
        author="Équipe technique",
    )
    width = A4[0] - 4 * cm
    story = []
    story += cover_flowables(width)
    story.append(PageBreak())
    story += content_flowables(width)
    doc.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    print(f"OK: {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build()

# Deriv Trading Bot — MVP Phase 1

Bot de trading pour indices synthétiques Deriv. Le bot tourne **côté serveur** (Python/FastAPI) et l'application mobile Flutter sert de **télécommande** (contrôle + monitoring temps réel).

## Architecture

```
┌────────────────┐   REST + WebSocket   ┌────────────────┐   WebSocket (wss)   ┌──────────────┐
│  Flutter App   │ ───────────────────▶ │  FastAPI (bot) │ ──────────────────▶ │  Deriv API   │
│  (mobile)      │  /api/bot/*          │  main.py       │  authorize/proposal │  ws.derivws  │
│                │ ◀─────────────────── │  bot_engine.py │  buy / open_contract│              │
│  Dashboard     │  /ws/bot/status (1s) │  deriv_client  │ ◀────────────────── │              │
└────────────────┘                      └────────────────┘   ticks / résultats └──────────────┘
```

- **`deriv_client.py`** : WebSocket Deriv (heartbeat, auto-reconnexion, `authorize`/`proposal`/`buy`).
- **`bot_engine.py`** : machine à états + Risk Manager (Daily Stop Loss / Take Profit calculés côté serveur, arrêt strict).
- **`main.py`** : API REST/WS exposée à l'app mobile.

## Étape 1 — Lancement du Backend

Prérequis : Python 3.11+.

```bash
cd backend
pip install -r requirements.txt
python main.py
```

Le serveur écoute sur `0.0.0.0:8000` :
- `POST /api/bot/start` — démarre le bot (`api_token`, `symbol`, `stake`, `stop_loss`, `take_profit`, `strategy_type`)
- `POST /api/bot/stop` — arrêt immédiat
- `GET  /api/bot/status` — snapshot (état + PnL)
- `WS   /ws/bot/status` — flux temps réel (push ~1s)
- `GET  /health` — sonde de vie

Vérification rapide :

```bash
curl http://localhost:8000/health
```

## Étape 2 — Configuration & Lancement du Mobile

Prérequis : Flutter 3.22+. Le dossier `mobile/` ne contient que `pubspec.yaml` + `lib/` ; il faut générer les dossiers de plateforme.

```bash
cd mobile
flutter create .
```

Ouvrir `android/app/build.gradle` et fixer le SDK minimum (requis par `flutter_secure_storage`) :

```gradle
android {
    defaultConfig {
        minSdkVersion 21
    }
}
```

Puis :

```bash
flutter pub get
flutter run
```

### Adresse du backend selon la cible

L'app pointe par défaut sur `10.0.2.2:8000` (alias de `localhost` de l'hôte depuis l'émulateur Android).

| Cible | Hôte à utiliser |
|---|---|
| Émulateur Android | `10.0.2.2:8000` (défaut) |
| Simulateur iOS | `127.0.0.1:8000` |
| Appareil physique | `IP_LAN_DU_PC:8000` (même réseau Wi-Fi) |

Pour un appareil physique, modifier le paramètre `host` de `BotService` dans `mobile/lib/services/bot_service.dart`.

## Étape 3 — Protocole de test MVP

1. **Générer un token API Deriv**
   - Se connecter sur [app.deriv.com](https://app.deriv.com) → **Settings → API token**.
   - Créer un token avec les scopes **Read** + **Trade**.
   - ⚠️ Utiliser un **compte Démo** pour le premier test (le bot passe des ordres réels sur un compte réel).

2. **Connexion**
   - Lancer l'app → coller le token → *Sauvegarder & Continuer* (stockage sécurisé Keystore/Keychain).

3. **Valider le flux WebSocket**
   - Le badge d'état et le PnL doivent se rafraîchir automatiquement (~1s), icône Wi-Fi verte = flux connecté.

4. **Valider les règles Daily SL/TP**
   - Configurer un **Stop Loss** et un **Take Profit** faibles (ex : SL=1, TP=1) pour déclencher rapidement.
   - Lancer le bot (`START`) et vérifier :
     - PnL vert si ≥ 0, rouge si < 0.
     - Liste des 5 derniers trades mise à jour (gain/perte).
     - Passage automatique du badge à **STOP_LOSS_REACHED** (rouge) ou **TAKE_PROFIT_REACHED** (bleu) dès le seuil atteint, **sans action manuelle** — l'arrêt est calculé et appliqué côté serveur.

## Structure du projet

```
trading/
├── backend/
│   ├── requirements.txt
│   ├── deriv_client.py   # client WebSocket Deriv
│   ├── bot_engine.py     # machine à états + risk manager
│   └── main.py           # API FastAPI (REST + WS)
└── mobile/
    ├── pubspec.yaml
    └── lib/
        ├── main.dart
        ├── services/bot_service.dart
        ├── providers/bot_provider.dart
        └── screens/
            ├── api_token_screen.dart
            └── dashboard_screen.dart
```

## Avertissement

Trading à haut risque. Le MVP Phase 1 utilise des stratégies volontairement simples (Rise/Fall, Over/Under) à but de démonstration technique — **pas de garantie de profit**. Tester exclusivement en compte Démo.

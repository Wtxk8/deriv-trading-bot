import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'providers/auth_provider.dart';
import 'providers/bot_provider.dart';
import 'screens/api_token_screen.dart';
import 'screens/dashboard_screen.dart';
import 'screens/login_screen.dart';
import 'theme/app_theme.dart';

void main() {
  runApp(const ProviderScope(child: DerivBotApp()));
}

class DerivBotApp extends StatelessWidget {
  const DerivBotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Deriv Trading Bot',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.build(),
      home: const _RootRouter(),
    );
  }
}

/// Routage initial : compte applicatif, puis token Deriv, puis tableau de bord.
///
/// L'ordre est imposé : le token API Deriv n'est jamais demandé avant qu'un
/// compte utilisateur soit créé et connecté. Ce routeur étant la racine de
/// l'app, aucun écran ne peut être fermé « sur du vide » : chaque changement
/// de session recalcule simplement l'écran affiché.
class _RootRouter extends ConsumerWidget {
  const _RootRouter();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final boot = ref.watch(sessionBootProvider);
    return boot.when(
      loading: () => const Scaffold(
        body: Center(child: CircularProgressIndicator()),
      ),
      // Stockage sécurisé illisible : on repart de la connexion.
      error: (_, __) => const LoginScreen(),
      data: (_) {
        // 1. Pas de session applicative valide : connexion (ou inscription).
        if (!ref.watch(hasValidSessionProvider)) return const LoginScreen();
        // 2. Compte connecté mais pas encore relié à Deriv : saisie du token.
        final token = ref.watch(tokenProvider);
        if (token == null || token.isEmpty) return const ApiTokenScreen();
        // 3. Compte + token : pilotage du robot.
        return const DashboardScreen();
      },
    );
  }
}

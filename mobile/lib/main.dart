import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'providers/bot_provider.dart';
import 'screens/api_token_screen.dart';
import 'screens/dashboard_screen.dart';

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
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.indigo),
        useMaterial3: true,
      ),
      home: const _RootRouter(),
    );
  }
}

/// Routage initial selon la présence d'un token en stockage sécurisé.
class _RootRouter extends ConsumerWidget {
  const _RootRouter();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final bootToken = ref.watch(bootTokenProvider);
    return bootToken.when(
      loading: () => const Scaffold(
        body: Center(child: CircularProgressIndicator()),
      ),
      error: (_, __) => const ApiTokenScreen(),
      data: (token) => (token != null && token.isNotEmpty)
          ? const DashboardScreen()
          : const ApiTokenScreen(),
    );
  }
}

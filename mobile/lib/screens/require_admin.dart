import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/auth_provider.dart';

/// Bloque l'accès à [child] si l'utilisateur courant n'a pas le rôle admin —
/// protège la route même en cas de navigation directe (hors menu).
class RequireAdmin extends ConsumerWidget {
  const RequireAdmin({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final isAdmin = ref.watch(isAdminProvider);
    if (!isAdmin) {
      return Scaffold(
        appBar: AppBar(title: const Text('Accès refusé')),
        body: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.lock_outline, size: 56, color: Colors.grey),
                const SizedBox(height: 16),
                const Text(
                  'Cette section est réservée aux administrateurs.',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 24),
                OutlinedButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: const Text('Retour'),
                ),
              ],
            ),
          ),
        ),
      );
    }
    return child;
  }
}

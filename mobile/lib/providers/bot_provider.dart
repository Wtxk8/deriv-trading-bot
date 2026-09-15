import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../services/bot_service.dart';
import 'auth_provider.dart';
import 'storage_provider.dart';

// Ré-export : les écrans qui lisent `secureStorageProvider` via ce fichier
// continuent de fonctionner.
export 'storage_provider.dart';

/// Clé de stockage sécurisé du token API Deriv.
const String kTokenKey = 'deriv_api_token';

/// Instance partagée du service backend.
final botServiceProvider = Provider<BotService>((ref) => BotService());

/// Notifier lisant/écrivant le token dans le stockage sécurisé.
class TokenNotifier extends StateNotifier<String?> {
  TokenNotifier(this._storage) : super(null) {
    _load();
  }

  final FlutterSecureStorage _storage;

  Future<void> _load() async {
    state = await _storage.read(key: kTokenKey);
  }

  Future<void> save(String token) async {
    final value = token.trim();
    await _storage.write(key: kTokenKey, value: value);
    state = value;
  }

  Future<void> clear() async {
    await _storage.delete(key: kTokenKey);
    state = null;
  }
}

final tokenProvider = StateNotifierProvider<TokenNotifier, String?>(
  (ref) => TokenNotifier(ref.watch(secureStorageProvider)),
);

/// Lecture unique du token au démarrage (routage initial).
final bootTokenProvider = FutureProvider<String?>(
  (ref) => ref.read(secureStorageProvider).read(key: kTokenKey),
);

/// Flux temps réel du statut du bot de l'utilisateur connecté.
///
/// Dépend du JWT : reconnecté à chaque changement de session, flux vide sans
/// JWT (le serveur exige une authentification). Auto-reconnexion gérée par le
/// service, sauf refus du JWT (fermeture 4401).
final botStatusStreamProvider =
    StreamProvider.autoDispose<Map<String, dynamic>>((ref) {
  final jwt = ref.watch(jwtProvider);
  if (jwt == null || jwt.isEmpty) {
    return const Stream<Map<String, dynamic>>.empty();
  }
  return ref.watch(botServiceProvider).connectStatusStream(jwt);
});

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../services/bot_service.dart';

/// Clé de stockage sécurisé du token API Deriv.
const String kTokenKey = 'deriv_api_token';

/// Instance partagée du service backend.
final botServiceProvider = Provider<BotService>((ref) => BotService());

/// Stockage sécurisé (Keystore Android / Keychain iOS).
final secureStorageProvider =
    Provider<FlutterSecureStorage>((ref) => const FlutterSecureStorage());

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

/// Flux temps réel du statut du bot (auto-reconnecté par le service).
final botStatusStreamProvider =
    StreamProvider.autoDispose<Map<String, dynamic>>(
  (ref) => ref.watch(botServiceProvider).connectStatusStream(),
);

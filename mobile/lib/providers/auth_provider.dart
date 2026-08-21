import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../services/auth_service.dart';
import '../services/jwt_utils.dart';
import 'bot_provider.dart';

/// Clé de stockage sécurisé du JWT de session (auth backend applicatif).
const String kJwtKey = 'auth_jwt';

final authServiceProvider = Provider<AuthService>((ref) => AuthService());

/// Notifier lisant/écrivant le JWT dans le stockage sécurisé.
class JwtNotifier extends StateNotifier<String?> {
  JwtNotifier(this._storage) : super(null) {
    _load();
  }

  final FlutterSecureStorage _storage;

  Future<void> _load() async {
    final value = await _storage.read(key: kJwtKey);
    if (value != null && JwtUtils.isExpired(value)) {
      await _storage.delete(key: kJwtKey);
      state = null;
      return;
    }
    state = value;
  }

  Future<void> save(String jwt) async {
    await _storage.write(key: kJwtKey, value: jwt);
    state = jwt;
  }

  Future<void> clear() async {
    await _storage.delete(key: kJwtKey);
    state = null;
  }
}

final jwtProvider = StateNotifierProvider<JwtNotifier, String?>(
  (ref) => JwtNotifier(ref.watch(secureStorageProvider)),
);

/// Rôle courant décodé depuis le JWT ('admin', 'user', ou null si absent/invalide).
final currentUserRoleProvider = Provider<String?>((ref) {
  final jwt = ref.watch(jwtProvider);
  if (jwt == null || jwt.isEmpty) return null;
  if (JwtUtils.isExpired(jwt)) return null;
  return JwtUtils.role(jwt);
});

final isAdminProvider = Provider<bool>((ref) {
  return ref.watch(currentUserRoleProvider) == 'admin';
});

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Stockage sécurisé (Keystore Android / Keychain iOS).
///
/// Fichier dédié, partagé par auth_provider et bot_provider : évite l'import
/// circulaire entre ces deux fichiers (bot_provider dépend du JWT).
final secureStorageProvider =
    Provider<FlutterSecureStorage>((ref) => const FlutterSecureStorage());

import 'dart:convert';

/// Décodage local (non vérifié) du payload d'un JWT — usage UI uniquement.
/// La validation de signature reste la responsabilité exclusive du backend.
class JwtUtils {
  const JwtUtils._();

  static Map<String, dynamic>? decodePayload(String token) {
    final parts = token.split('.');
    if (parts.length != 3) return null;
    try {
      final normalized = base64Url.normalize(parts[1]);
      final decoded = utf8.decode(base64Url.decode(normalized));
      final payload = jsonDecode(decoded);
      return payload is Map<String, dynamic> ? payload : null;
    } catch (_) {
      return null;
    }
  }

  static String? role(String token) =>
      decodePayload(token)?['role'] as String?;

  static bool isExpired(String token) {
    final exp = decodePayload(token)?['exp'];
    if (exp is! num) return false;
    final expiry = DateTime.fromMillisecondsSinceEpoch(exp.toInt() * 1000);
    return DateTime.now().isAfter(expiry);
  }
}

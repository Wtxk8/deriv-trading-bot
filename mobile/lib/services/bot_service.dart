import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

/// Client HTTP + WebSocket vers le backend FastAPI (via tunnel Cloudflare).
class BotService {
  BotService({this.host = 'api1.innovahub226.com'});

  /// Hôte (sans schéma, sans port — routé via le tunnel Cloudflare Access).
  final String host;

  /// Code de fermeture WebSocket du serveur quand le JWT est refusé/expiré.
  static const int wsUnauthorizedCode = 4401;

  Uri _http(String path) => Uri.parse('https://$host$path');
  Uri _ws(String path) => Uri.parse('wss://$host$path');

  static const Map<String, String> _jsonHeaders = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  };

  /// En-têtes JSON, avec `Authorization: Bearer <jwt>` si un JWT est fourni.
  Map<String, String> _headers(String? jwt) {
    final headers = Map<String, String>.from(_jsonHeaders);
    if (jwt != null && jwt.isNotEmpty) {
      headers['Authorization'] = 'Bearer $jwt';
    }
    return headers;
  }

  Future<Map<String, dynamic>> startBot({
    required String token,
    required String symbol,
    required double stake,
    required double stopLoss,
    required double takeProfit,
    required String strategy,
    String accountType = 'demo',
    String? jwt,
  }) async {
    final response = await http
        .post(
          _http('/api/bot/start'),
          headers: _headers(jwt),
          body: jsonEncode(<String, dynamic>{
            'api_token': token,
            'symbol': symbol,
            'stake': stake,
            'stop_loss': stopLoss,
            'take_profit': takeProfit,
            'strategy_type': strategy,
            'account_type': accountType,
          }),
        )
        .timeout(const Duration(seconds: 20));
    return _decode(response);
  }

  Future<Map<String, dynamic>> stopBot({String? jwt}) async {
    final response = await http
        .post(_http('/api/bot/stop'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 20));
    return _decode(response);
  }

  /// URL d'inscription Deriv avec le token d'affiliation IB (partenariat).
  Future<String> getDerivSignupUrl() async {
    final response = await http
        .get(_http('/affiliate/deriv'), headers: _jsonHeaders)
        .timeout(const Duration(seconds: 10));
    final body = _decode(response);
    final url = body['url'] as String?;
    if (url == null || url.isEmpty) {
      throw BotServiceException(500, 'URL affiliation vide');
    }
    return url;
  }

  Future<Map<String, dynamic>> getStatus({String? jwt}) async {
    final response = await http
        .get(_http('/api/bot/status'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    return _decode(response);
  }

  /// Flux temps réel du statut du bot de l'utilisateur (JWT requis).
  ///
  /// Après connexion, le client s'authentifie par un premier message
  /// `{"type":"auth","token":jwt}` ; l'accusé `{"type":"auth_ok"}` est ignoré.
  /// Auto-reconnexion (délai 2s) sur coupure, sauf fermeture 4401 (JWT
  /// refusé) : le flux s'arrête, inutile de boucler.
  Stream<Map<String, dynamic>> connectStatusStream(String jwt) async* {
    while (true) {
      WebSocketChannel? channel;
      try {
        channel = WebSocketChannel.connect(_ws('/ws/bot/status'));
        await channel.ready;
        channel.sink.add(
          jsonEncode(<String, dynamic>{'type': 'auth', 'token': jwt}),
        );
        await for (final dynamic message in channel.stream) {
          final decoded = jsonDecode(message as String);
          if (decoded is Map<String, dynamic>) {
            if (decoded['type'] == 'auth_ok') continue;
            yield decoded;
          }
        }
      } catch (_) {
        // Connexion perdue : on retente après un court délai.
      } finally {
        await channel?.sink.close();
      }
      if (channel?.closeCode == wsUnauthorizedCode) {
        // JWT refusé ou expiré : fin du flux (reconnexion via un nouveau JWT).
        return;
      }
      await Future<void>.delayed(const Duration(seconds: 2));
    }
  }

  Map<String, dynamic> _decode(http.Response response) {
    final dynamic body =
        response.body.isEmpty ? <String, dynamic>{} : jsonDecode(response.body);
    if (response.statusCode >= 400) {
      final detail = body is Map<String, dynamic>
          ? (body['detail'] ?? body.toString())
          : body.toString();
      throw BotServiceException(response.statusCode, detail.toString());
    }
    return body is Map<String, dynamic> ? body : <String, dynamic>{};
  }
}

class BotServiceException implements Exception {
  BotServiceException(this.statusCode, this.detail);

  final int statusCode;
  final String detail;

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

import 'dart:convert';

import 'package:http/http.dart' as http;

/// Client HTTP pour l'authentification (login) vers le backend.
class AuthService {
  AuthService({this.baseUrl = 'https://api1.innovahub226.com'});

  final String baseUrl;

  Future<String> login(String email, String password) async {
    final response = await http
        .post(
          Uri.parse('$baseUrl/login'),
          headers: const {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
          },
          body: jsonEncode(<String, String>{
            'email': email,
            'password': password,
          }),
        )
        .timeout(const Duration(seconds: 20));

    final dynamic body =
        response.body.isEmpty ? <String, dynamic>{} : jsonDecode(response.body);

    if (response.statusCode >= 400) {
      final detail = body is Map<String, dynamic>
          ? (body['detail'] ?? body.toString())
          : body.toString();
      throw AuthServiceException(response.statusCode, detail.toString());
    }

    return (body as Map<String, dynamic>)['access_token'] as String;
  }
}

class AuthServiceException implements Exception {
  AuthServiceException(this.statusCode, this.detail);

  final int statusCode;
  final String detail;

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

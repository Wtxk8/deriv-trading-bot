import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/admin_user.dart';

/// Client HTTP vers l'API d'administration des utilisateurs.
class AdminService {
  AdminService({this.baseUrl = 'https://api1.innovahub226.com'});

  final String baseUrl;

  Map<String, String> _headers(String jwt) => {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': 'Bearer $jwt',
      };

  Future<List<AdminUser>> fetchUsers(String jwt) async {
    final response = await http
        .get(Uri.parse('$baseUrl/admin/users'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 20));
    final body = _decode(response);
    final list = body is List ? body : (body['users'] as List? ?? const []);
    return list
        .whereType<Map<String, dynamic>>()
        .map(AdminUser.fromJson)
        .toList(growable: false);
  }

  Future<AdminUser> updateUser(
    String jwt,
    String id, {
    String? role,
    bool? active,
  }) async {
    final response = await http
        .patch(
          Uri.parse('$baseUrl/admin/users/$id'),
          headers: _headers(jwt),
          body: jsonEncode(<String, dynamic>{
            if (role != null) 'role': role,
            if (active != null) 'active': active,
          }),
        )
        .timeout(const Duration(seconds: 20));
    return AdminUser.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<void> deleteUser(String jwt, String id) async {
    final response = await http
        .delete(Uri.parse('$baseUrl/admin/users/$id'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 20));
    if (response.statusCode >= 400) {
      throw AdminServiceException(response.statusCode, response.body);
    }
  }

  dynamic _decode(http.Response response) {
    final dynamic body =
        response.body.isEmpty ? <String, dynamic>{} : jsonDecode(response.body);
    if (response.statusCode >= 400) {
      final detail = body is Map<String, dynamic>
          ? (body['detail'] ?? body.toString())
          : body.toString();
      throw AdminServiceException(response.statusCode, detail.toString());
    }
    return body;
  }
}

class AdminServiceException implements Exception {
  AdminServiceException(this.statusCode, this.detail);

  final int statusCode;
  final String detail;

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

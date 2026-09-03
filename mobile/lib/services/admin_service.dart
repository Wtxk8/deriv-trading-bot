import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/admin_user.dart';

/// Client HTTP vers l'API d'administration (users + abonnements + stats).
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

  Future<AdminStats> fetchStats(String jwt) async {
    final response = await http
        .get(Uri.parse('$baseUrl/admin/stats'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    return AdminStats.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<AdminUser> updateUser(
    String jwt,
    String id, {
    String? role,
    bool? active,
    String? name,
    String? email,
  }) async {
    final response = await http
        .patch(
          Uri.parse('$baseUrl/admin/users/$id'),
          headers: _headers(jwt),
          body: jsonEncode(<String, dynamic>{
            if (role != null) 'role': role,
            if (active != null) 'active': active,
            if (name != null) 'name': name,
            if (email != null) 'email': email,
          }),
        )
        .timeout(const Duration(seconds: 20));
    return AdminUser.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<AdminUser> grantPremium(String jwt, String id, int days) async {
    final response = await http
        .post(
          Uri.parse('$baseUrl/admin/users/$id/grant-premium'),
          headers: _headers(jwt),
          body: jsonEncode(<String, int>{'days': days}),
        )
        .timeout(const Duration(seconds: 15));
    return AdminUser.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<AdminUser> revokePremium(String jwt, String id) async {
    final response = await http
        .post(Uri.parse('$baseUrl/admin/users/$id/revoke-premium'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    return AdminUser.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<AdminUser> resetTrial(String jwt, String id) async {
    final response = await http
        .post(Uri.parse('$baseUrl/admin/users/$id/reset-trial'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    return AdminUser.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<String> resetPassword(String jwt, String id) async {
    final response = await http
        .post(Uri.parse('$baseUrl/admin/users/$id/reset-password'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    final data = _decode(response) as Map<String, dynamic>;
    return (data['new_password'] as String?) ?? '';
  }

  Future<List<AdminPayment>> fetchUserPayments(String jwt, String id) async {
    final response = await http
        .get(Uri.parse('$baseUrl/admin/users/$id/payments'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    final body = _decode(response);
    final list = body is List ? body : const [];
    return list.whereType<Map<String, dynamic>>().map(AdminPayment.fromJson).toList(growable: false);
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

class AdminStats {
  const AdminStats({
    required this.usersTotal,
    required this.usersActive,
    required this.usersSuspended,
    required this.adminsTotal,
    required this.trialActive,
    required this.premiumActive,
    required this.paymentsPaid,
    required this.revenueXofTotal,
    required this.revenueXof30d,
  });

  final int usersTotal;
  final int usersActive;
  final int usersSuspended;
  final int adminsTotal;
  final int trialActive;
  final int premiumActive;
  final int paymentsPaid;
  final int revenueXofTotal;
  final int revenueXof30d;

  factory AdminStats.fromJson(Map<String, dynamic> json) {
    int i(String k) => (json[k] as num?)?.toInt() ?? 0;
    return AdminStats(
      usersTotal: i('users_total'),
      usersActive: i('users_active'),
      usersSuspended: i('users_suspended'),
      adminsTotal: i('admins_total'),
      trialActive: i('trial_active'),
      premiumActive: i('premium_active'),
      paymentsPaid: i('payments_paid'),
      revenueXofTotal: i('revenue_xof_total'),
      revenueXof30d: i('revenue_xof_30d'),
    );
  }
}

class AdminPayment {
  const AdminPayment({
    required this.id,
    required this.plan,
    required this.provider,
    required this.amountXof,
    required this.status,
    required this.createdAt,
    this.paidAt,
  });

  final int id;
  final String plan;
  final String provider;
  final int amountXof;
  final String status;
  final DateTime createdAt;
  final DateTime? paidAt;

  factory AdminPayment.fromJson(Map<String, dynamic> json) {
    DateTime parse(dynamic v) => v is String ? (DateTime.tryParse(v) ?? DateTime.now()) : DateTime.now();
    DateTime? maybe(dynamic v) => v is String ? DateTime.tryParse(v) : null;
    return AdminPayment(
      id: (json['id'] as num?)?.toInt() ?? 0,
      plan: (json['plan'] as String?) ?? '',
      provider: (json['provider'] as String?) ?? '',
      amountXof: (json['amount_xof'] as num?)?.toInt() ?? 0,
      status: (json['status'] as String?) ?? 'pending',
      createdAt: parse(json['created_at']),
      paidAt: maybe(json['paid_at']),
    );
  }
}

class AdminServiceException implements Exception {
  AdminServiceException(this.statusCode, this.detail);

  final int statusCode;
  final String detail;

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

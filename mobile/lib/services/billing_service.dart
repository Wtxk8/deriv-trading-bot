import 'dart:convert';

import 'package:http/http.dart' as http;

/// Client HTTP vers l'API de facturation (essai, plans, checkout).
class BillingService {
  BillingService({this.baseUrl = 'https://api1.innovahub226.com'});

  final String baseUrl;

  Map<String, String> _headers([String? jwt]) => {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        if (jwt != null && jwt.isNotEmpty) 'Authorization': 'Bearer $jwt',
      };

  Future<SubscriptionStatus> fetchStatus(String jwt) async {
    final response = await http
        .get(Uri.parse('$baseUrl/billing/status'), headers: _headers(jwt))
        .timeout(const Duration(seconds: 15));
    return SubscriptionStatus.fromJson(_decode(response) as Map<String, dynamic>);
  }

  Future<Map<String, PlanInfo>> fetchPlans() async {
    final response = await http
        .get(Uri.parse('$baseUrl/billing/plans'), headers: _headers())
        .timeout(const Duration(seconds: 15));
    final body = _decode(response) as Map<String, dynamic>;
    final plans = (body['plans'] as Map<String, dynamic>? ?? const {});
    return plans.map((k, v) => MapEntry(k, PlanInfo.fromJson(k, v as Map<String, dynamic>)));
  }

  /// Initie un paiement Mobile Money. Renvoie l'URL de checkout à ouvrir.
  Future<CheckoutSession> initCheckout({
    required String jwt,
    required String plan,
    required String provider,
    String? phone,
  }) async {
    final response = await http
        .post(
          Uri.parse('$baseUrl/billing/checkout'),
          headers: _headers(jwt),
          body: jsonEncode(<String, dynamic>{
            'plan': plan,
            'provider': provider,
            if (phone != null && phone.isNotEmpty) 'phone': phone,
          }),
        )
        .timeout(const Duration(seconds: 30));
    return CheckoutSession.fromJson(_decode(response) as Map<String, dynamic>);
  }

  dynamic _decode(http.Response response) {
    final dynamic body =
        response.body.isEmpty ? <String, dynamic>{} : jsonDecode(response.body);
    if (response.statusCode >= 400) {
      final detail = body is Map<String, dynamic>
          ? (body['detail'] ?? body.toString())
          : body.toString();
      throw BillingServiceException(response.statusCode, detail.toString());
    }
    return body;
  }
}

class SubscriptionStatus {
  const SubscriptionStatus({
    required this.tier,
    required this.premiumActive,
    required this.premiumExpiresAt,
    required this.trialActive,
    required this.trialExpiresAt,
    required this.trialDaysRemaining,
    required this.canTradeReal,
  });

  final String tier; // free | premium
  final bool premiumActive;
  final DateTime? premiumExpiresAt;
  final bool trialActive;
  final DateTime? trialExpiresAt;
  final int trialDaysRemaining;
  final bool canTradeReal;

  factory SubscriptionStatus.fromJson(Map<String, dynamic> json) {
    DateTime? parse(dynamic v) => v is String ? DateTime.tryParse(v) : null;
    return SubscriptionStatus(
      tier: (json['tier'] as String?) ?? 'free',
      premiumActive: (json['premium_active'] as bool?) ?? false,
      premiumExpiresAt: parse(json['premium_expires_at']),
      trialActive: (json['trial_active'] as bool?) ?? false,
      trialExpiresAt: parse(json['trial_expires_at']),
      trialDaysRemaining: (json['trial_days_remaining'] as num?)?.toInt() ?? 0,
      canTradeReal: (json['can_trade_real'] as bool?) ?? false,
    );
  }
}

class PlanInfo {
  const PlanInfo({
    required this.key,
    required this.label,
    required this.amountXof,
    required this.durationDays,
  });

  final String key;
  final String label;
  final int amountXof;
  final int durationDays;

  factory PlanInfo.fromJson(String key, Map<String, dynamic> json) {
    return PlanInfo(
      key: key,
      label: (json['label'] as String?) ?? key,
      amountXof: (json['amount_xof'] as num?)?.toInt() ?? 0,
      durationDays: (json['duration_days'] as num?)?.toInt() ?? 0,
    );
  }
}

class CheckoutSession {
  const CheckoutSession({required this.id, required this.checkoutUrl, required this.status});
  final int id;
  final String checkoutUrl;
  final String status;

  factory CheckoutSession.fromJson(Map<String, dynamic> json) {
    return CheckoutSession(
      id: (json['id'] as num?)?.toInt() ?? 0,
      checkoutUrl: (json['checkout_url'] as String?) ?? '',
      status: (json['status'] as String?) ?? 'pending',
    );
  }
}

class BillingServiceException implements Exception {
  BillingServiceException(this.statusCode, this.detail);
  final int statusCode;
  final String detail;

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

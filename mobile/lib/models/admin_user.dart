class AdminUser {
  const AdminUser({
    required this.id,
    required this.name,
    required this.email,
    required this.role,
    required this.active,
    this.subscriptionTier = 'free',
    this.subscriptionExpiresAt,
    this.trialStartedAt,
  });

  factory AdminUser.fromJson(Map<String, dynamic> json) {
    DateTime? parse(dynamic v) => v is String ? DateTime.tryParse(v) : null;
    return AdminUser(
      id: json['id'].toString(),
      name: (json['name'] as String?) ?? '',
      email: (json['email'] as String?) ?? '',
      role: (json['role'] as String?) ?? 'user',
      active: (json['active'] as bool?) ?? (json['status'] == 'active'),
      subscriptionTier: (json['subscription_tier'] as String?) ?? 'free',
      subscriptionExpiresAt: parse(json['subscription_expires_at']),
      trialStartedAt: parse(json['trial_started_at']),
    );
  }

  final String id;
  final String name;
  final String email;
  final String role;
  final bool active;
  final String subscriptionTier;
  final DateTime? subscriptionExpiresAt;
  final DateTime? trialStartedAt;

  bool get isAdmin => role == 'admin';

  bool get isPremiumActive {
    if (subscriptionTier != 'premium') return false;
    if (subscriptionExpiresAt == null) return true;
    return subscriptionExpiresAt!.isAfter(DateTime.now().toUtc());
  }

  bool get isTrialActive {
    if (trialStartedAt == null) return false;
    final end = trialStartedAt!.add(const Duration(days: 7));
    return DateTime.now().toUtc().isBefore(end);
  }

  int get trialDaysLeft {
    if (trialStartedAt == null) return 0;
    final end = trialStartedAt!.add(const Duration(days: 7));
    final diff = end.difference(DateTime.now().toUtc());
    if (diff.isNegative) return 0;
    return diff.inHours ~/ 24 + (diff.inHours % 24 > 0 ? 1 : 0);
  }

  AdminUser copyWith({
    String? role,
    bool? active,
    String? subscriptionTier,
    DateTime? subscriptionExpiresAt,
    DateTime? trialStartedAt,
  }) =>
      AdminUser(
        id: id,
        name: name,
        email: email,
        role: role ?? this.role,
        active: active ?? this.active,
        subscriptionTier: subscriptionTier ?? this.subscriptionTier,
        subscriptionExpiresAt: subscriptionExpiresAt ?? this.subscriptionExpiresAt,
        trialStartedAt: trialStartedAt ?? this.trialStartedAt,
      );
}

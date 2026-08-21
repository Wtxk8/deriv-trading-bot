class AdminUser {
  const AdminUser({
    required this.id,
    required this.name,
    required this.email,
    required this.role,
    required this.active,
  });

  factory AdminUser.fromJson(Map<String, dynamic> json) {
    return AdminUser(
      id: json['id'].toString(),
      name: (json['name'] as String?) ?? '',
      email: (json['email'] as String?) ?? '',
      role: (json['role'] as String?) ?? 'user',
      active: (json['active'] as bool?) ?? (json['status'] == 'active'),
    );
  }

  final String id;
  final String name;
  final String email;
  final String role;
  final bool active;

  bool get isAdmin => role == 'admin';

  AdminUser copyWith({String? role, bool? active}) => AdminUser(
        id: id,
        name: name,
        email: email,
        role: role ?? this.role,
        active: active ?? this.active,
      );
}

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/admin_user.dart';
import '../services/admin_service.dart';
import 'auth_provider.dart';

final adminServiceProvider = Provider<AdminService>((ref) => AdminService());

class AdminUsersState {
  const AdminUsersState({
    this.users = const [],
    this.loading = false,
    this.error,
  });

  final List<AdminUser> users;
  final bool loading;
  final String? error;

  AdminUsersState copyWith({
    List<AdminUser>? users,
    bool? loading,
    String? error,
  }) =>
      AdminUsersState(
        users: users ?? this.users,
        loading: loading ?? this.loading,
        error: error,
      );
}

class AdminUsersNotifier extends StateNotifier<AdminUsersState> {
  AdminUsersNotifier(this._service, this._jwt) : super(const AdminUsersState());

  final AdminService _service;
  final String? _jwt;

  Future<void> load() async {
    final jwt = _jwt;
    if (jwt == null) {
      state = state.copyWith(error: 'Session expirée', loading: false);
      return;
    }
    state = state.copyWith(loading: true, error: null);
    try {
      final users = await _service.fetchUsers(jwt);
      state = state.copyWith(users: users, loading: false);
    } on AdminServiceException catch (e) {
      state = state.copyWith(error: e.toString(), loading: false);
    } catch (e) {
      state = state.copyWith(error: 'Échec du chargement : $e', loading: false);
    }
  }

  Future<bool> changeRole(String id, String newRole) =>
      _mutate(id, () => _service.updateUser(_jwt!, id, role: newRole));

  Future<bool> setActive(String id, bool active) =>
      _mutate(id, () => _service.updateUser(_jwt!, id, active: active));

  Future<bool> deleteUser(String id) async {
    final jwt = _jwt;
    if (jwt == null) return false;
    try {
      await _service.deleteUser(jwt, id);
      state = state.copyWith(
        users: state.users.where((u) => u.id != id).toList(growable: false),
      );
      return true;
    } catch (e) {
      state = state.copyWith(error: 'Échec suppression : $e');
      return false;
    }
  }

  Future<bool> _mutate(String id, Future<AdminUser> Function() action) async {
    if (_jwt == null) return false;
    try {
      final updated = await action();
      state = state.copyWith(
        users: [
          for (final u in state.users) if (u.id == id) updated else u,
        ],
      );
      return true;
    } catch (e) {
      state = state.copyWith(error: 'Échec mise à jour : $e');
      return false;
    }
  }
}

final adminUsersProvider =
    StateNotifierProvider<AdminUsersNotifier, AdminUsersState>((ref) {
  return AdminUsersNotifier(
    ref.watch(adminServiceProvider),
    ref.watch(jwtProvider),
  );
});

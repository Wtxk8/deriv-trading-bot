import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/admin_user.dart';
import '../services/admin_service.dart';
import 'auth_provider.dart';

final adminServiceProvider = Provider<AdminService>((ref) => AdminService());

class AdminUsersState {
  const AdminUsersState({
    this.users = const [],
    this.stats,
    this.loading = false,
    this.error,
  });

  final List<AdminUser> users;
  final AdminStats? stats;
  final bool loading;
  final String? error;

  AdminUsersState copyWith({
    List<AdminUser>? users,
    AdminStats? stats,
    bool? loading,
    String? error,
  }) =>
      AdminUsersState(
        users: users ?? this.users,
        stats: stats ?? this.stats,
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
      final results = await Future.wait([
        _service.fetchUsers(jwt),
        _service.fetchStats(jwt),
      ]);
      state = state.copyWith(
        users: results[0] as List<AdminUser>,
        stats: results[1] as AdminStats,
        loading: false,
      );
    } on AdminServiceException catch (e) {
      state = state.copyWith(error: e.toString(), loading: false);
    } catch (e) {
      state = state.copyWith(error: 'Échec du chargement : $e', loading: false);
    }
  }

  Future<void> _refreshStats() async {
    final jwt = _jwt;
    if (jwt == null) return;
    try {
      final stats = await _service.fetchStats(jwt);
      state = state.copyWith(stats: stats);
    } catch (_) {}
  }

  Future<bool> changeRole(String id, String newRole) =>
      _mutate(id, () => _service.updateUser(_jwt!, id, role: newRole));

  Future<bool> setActive(String id, bool active) =>
      _mutate(id, () => _service.updateUser(_jwt!, id, active: active));

  Future<bool> grantPremium(String id, int days) =>
      _mutate(id, () => _service.grantPremium(_jwt!, id, days));

  Future<bool> revokePremium(String id) =>
      _mutate(id, () => _service.revokePremium(_jwt!, id));

  Future<bool> resetTrial(String id) =>
      _mutate(id, () => _service.resetTrial(_jwt!, id));

  Future<String?> resetPassword(String id) async {
    final jwt = _jwt;
    if (jwt == null) return null;
    try {
      return await _service.resetPassword(jwt, id);
    } catch (e) {
      state = state.copyWith(error: 'Échec réinitialisation : $e');
      return null;
    }
  }

  Future<List<AdminPayment>> fetchUserPayments(String id) async {
    final jwt = _jwt;
    if (jwt == null) return const [];
    try {
      return await _service.fetchUserPayments(jwt, id);
    } catch (_) {
      return const [];
    }
  }

  Future<bool> deleteUser(String id) async {
    final jwt = _jwt;
    if (jwt == null) return false;
    try {
      await _service.deleteUser(jwt, id);
      state = state.copyWith(
        users: state.users.where((u) => u.id != id).toList(growable: false),
      );
      await _refreshStats();
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
      await _refreshStats();
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

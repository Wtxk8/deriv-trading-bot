import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/admin_user.dart';
import '../providers/admin_provider.dart';

/// Liste, recherche et administration des comptes utilisateurs.
class AdminUserManagementScreen extends ConsumerStatefulWidget {
  const AdminUserManagementScreen({super.key});

  @override
  ConsumerState<AdminUserManagementScreen> createState() =>
      _AdminUserManagementScreenState();
}

class _AdminUserManagementScreenState
    extends ConsumerState<AdminUserManagementScreen> {
  final TextEditingController _search = TextEditingController();
  String _query = '';

  @override
  void initState() {
    super.initState();
    Future.microtask(() => ref.read(adminUsersProvider.notifier).load());
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  List<AdminUser> _filter(List<AdminUser> users) {
    final q = _query.trim().toLowerCase();
    if (q.isEmpty) return users;
    return users
        .where((u) =>
            u.name.toLowerCase().contains(q) ||
            u.email.toLowerCase().contains(q))
        .toList(growable: false);
  }

  Future<bool> _confirm({
    required String title,
    required String message,
    bool destructive = false,
  }) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Annuler'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            style: destructive
                ? FilledButton.styleFrom(backgroundColor: Colors.red)
                : null,
            child: const Text('Confirmer'),
          ),
        ],
      ),
    );
    return result ?? false;
  }

  void _snack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _toggleRole(AdminUser user) async {
    final newRole = user.isAdmin ? 'user' : 'admin';
    final ok = await _confirm(
      title: 'Changer le rôle',
      message:
          '${user.name} passera de "${user.role}" à "$newRole". Continuer ?',
    );
    if (!ok) return;
    final success =
        await ref.read(adminUsersProvider.notifier).changeRole(user.id, newRole);
    _snack(success ? 'Rôle mis à jour' : 'Échec de la mise à jour du rôle');
  }

  Future<void> _toggleActive(AdminUser user) async {
    final activate = !user.active;
    final ok = await _confirm(
      title: activate ? 'Réactiver le compte' : 'Suspendre le compte',
      message:
          '${activate ? 'Réactiver' : 'Suspendre'} le compte de ${user.name} ?',
      destructive: !activate,
    );
    if (!ok) return;
    final success =
        await ref.read(adminUsersProvider.notifier).setActive(user.id, activate);
    _snack(success ? 'Statut mis à jour' : 'Échec de la mise à jour du statut');
  }

  Future<void> _delete(AdminUser user) async {
    final ok = await _confirm(
      title: 'Supprimer le compte',
      message:
          'Supprimer définitivement le compte de ${user.name} (${user.email}) ? Cette action est irréversible.',
      destructive: true,
    );
    if (!ok) return;
    final success = await ref.read(adminUsersProvider.notifier).deleteUser(user.id);
    _snack(success ? 'Compte supprimé' : 'Échec de la suppression');
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(adminUsersProvider);
    final users = _filter(state.users);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Gestion des utilisateurs'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: 'Rafraîchir',
            onPressed: () => ref.read(adminUsersProvider.notifier).load(),
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
            child: TextField(
              controller: _search,
              onChanged: (v) => setState(() => _query = v),
              decoration: InputDecoration(
                hintText: 'Rechercher par nom ou email',
                prefixIcon: const Icon(Icons.search),
                border: const OutlineInputBorder(),
                isDense: true,
                suffixIcon: _query.isEmpty
                    ? null
                    : IconButton(
                        icon: const Icon(Icons.clear),
                        onPressed: () => setState(() {
                          _search.clear();
                          _query = '';
                        }),
                      ),
              ),
            ),
          ),
          if (state.error != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
              child: Text(state.error!, style: const TextStyle(color: Colors.red)),
            ),
          Expanded(
            child: state.loading
                ? const Center(child: CircularProgressIndicator())
                : users.isEmpty
                    ? const Center(child: Text('Aucun utilisateur'))
                    : RefreshIndicator(
                        onRefresh: () => ref.read(adminUsersProvider.notifier).load(),
                        child: ListView.separated(
                          padding: const EdgeInsets.all(16),
                          itemCount: users.length,
                          separatorBuilder: (_, __) => const SizedBox(height: 8),
                          itemBuilder: (context, i) =>
                              _UserCard(
                            user: users[i],
                            onToggleRole: () => _toggleRole(users[i]),
                            onToggleActive: () => _toggleActive(users[i]),
                            onDelete: () => _delete(users[i]),
                          ),
                        ),
                      ),
          ),
        ],
      ),
    );
  }
}

class _UserCard extends StatelessWidget {
  const _UserCard({
    required this.user,
    required this.onToggleRole,
    required this.onToggleActive,
    required this.onDelete,
  });

  final AdminUser user;
  final VoidCallback onToggleRole;
  final VoidCallback onToggleActive;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                CircleAvatar(child: Text(user.name.isNotEmpty ? user.name[0].toUpperCase() : '?')),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(user.name, style: const TextStyle(fontWeight: FontWeight.bold)),
                      Text(user.email, style: const TextStyle(color: Colors.grey, fontSize: 13)),
                    ],
                  ),
                ),
                _StatusChip(active: user.active),
                const SizedBox(width: 6),
                _RoleChip(role: user.role),
              ],
            ),
            const Divider(height: 20),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton.icon(
                  onPressed: onToggleRole,
                  icon: const Icon(Icons.admin_panel_settings_outlined, size: 18),
                  label: Text(user.isAdmin ? 'Rétrograder' : 'Promouvoir admin'),
                ),
                TextButton.icon(
                  onPressed: onToggleActive,
                  icon: Icon(
                    user.active ? Icons.block : Icons.check_circle_outline,
                    size: 18,
                  ),
                  label: Text(user.active ? 'Suspendre' : 'Activer'),
                ),
                IconButton(
                  onPressed: onDelete,
                  icon: const Icon(Icons.delete_outline, color: Colors.red),
                  tooltip: 'Supprimer',
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _RoleChip extends StatelessWidget {
  const _RoleChip({required this.role});
  final String role;

  @override
  Widget build(BuildContext context) {
    final isAdmin = role == 'admin';
    return Chip(
      label: Text(role),
      backgroundColor: (isAdmin ? Colors.indigo : Colors.grey).withValues(alpha: 0.15),
      labelStyle: TextStyle(color: isAdmin ? Colors.indigo : Colors.grey.shade700, fontSize: 12),
      visualDensity: VisualDensity.compact,
      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
    );
  }
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.active});
  final bool active;

  @override
  Widget build(BuildContext context) {
    final color = active ? Colors.green : Colors.red;
    return Chip(
      label: Text(active ? 'actif' : 'suspendu'),
      backgroundColor: color.withValues(alpha: 0.15),
      labelStyle: TextStyle(color: color, fontSize: 12),
      visualDensity: VisualDensity.compact,
      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
    );
  }
}

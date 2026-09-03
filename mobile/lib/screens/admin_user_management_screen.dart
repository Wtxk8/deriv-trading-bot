import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../models/admin_user.dart';
import '../providers/admin_provider.dart';
import '../services/admin_service.dart';
import '../theme/app_theme.dart';

/// Console administrateur mobile : stats globales + gestion complète des users.
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
  String _filter = 'all'; // all | admin | premium | trial | expired | suspended

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

  List<AdminUser> _apply(List<AdminUser> users) {
    final q = _query.trim().toLowerCase();
    return users.where((u) {
      if (q.isNotEmpty && !u.name.toLowerCase().contains(q) && !u.email.toLowerCase().contains(q)) {
        return false;
      }
      switch (_filter) {
        case 'admin':
          return u.isAdmin;
        case 'premium':
          return u.isPremiumActive;
        case 'trial':
          return u.isTrialActive;
        case 'expired':
          return !u.isPremiumActive && !u.isTrialActive;
        case 'suspended':
          return !u.active;
        default:
          return true;
      }
    }).toList(growable: false);
  }

  void _snack(String msg, {bool ok = false, bool err = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: err
          ? AppColors.danger
          : ok
              ? AppColors.success
              : null,
    ));
  }

  Future<bool> _confirm(String title, String message, {bool destructive = false}) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppColors.surface,
        title: Text(title, style: AppTheme.heading(fontSize: 16)),
        content: Text(message, style: GoogleFonts.manrope(fontSize: 13.5, color: AppColors.textSecondary)),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Annuler')),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: destructive ? FilledButton.styleFrom(backgroundColor: AppColors.danger) : null,
            child: const Text('Confirmer'),
          ),
        ],
      ),
    );
    return ok ?? false;
  }

  Future<void> _grantPremium(AdminUser user) async {
    final days = await showDialog<int>(
      context: context,
      builder: (ctx) => _GrantPremiumDialog(userName: user.name.isEmpty ? user.email : user.name),
    );
    if (days == null) return;
    final ok = await ref.read(adminUsersProvider.notifier).grantPremium(user.id, days);
    _snack(ok ? '$days jours de premium accordés' : 'Échec activation premium', ok: ok, err: !ok);
  }

  Future<void> _revokePremium(AdminUser user) async {
    if (!await _confirm(
      'Retirer le premium',
      'Le compte de ${user.name.isEmpty ? user.email : user.name} repassera en free.',
      destructive: true,
    )) return;
    final ok = await ref.read(adminUsersProvider.notifier).revokePremium(user.id);
    _snack(ok ? 'Premium retiré' : 'Échec', ok: ok, err: !ok);
  }

  Future<void> _resetTrial(AdminUser user) async {
    if (!await _confirm(
      'Redémarrer l\'essai',
      'Réinitialise le compteur à 7 jours pleins pour ${user.name.isEmpty ? user.email : user.name}.',
    )) return;
    final ok = await ref.read(adminUsersProvider.notifier).resetTrial(user.id);
    _snack(ok ? 'Essai redémarré (7 jours)' : 'Échec', ok: ok, err: !ok);
  }

  Future<void> _resetPassword(AdminUser user) async {
    if (!await _confirm(
      'Réinitialiser le mot de passe',
      'Un nouveau mot de passe sera généré pour ${user.email}. À lui transmettre par un canal sûr.',
    )) return;
    final newPwd = await ref.read(adminUsersProvider.notifier).resetPassword(user.id);
    if (!mounted || newPwd == null || newPwd.isEmpty) {
      _snack('Échec de la réinitialisation', err: true);
      return;
    }
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppColors.surface,
        title: Text('Nouveau mot de passe', style: AppTheme.heading(fontSize: 16)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Transmettez ce mot de passe à ${user.email}.',
                style: GoogleFonts.manrope(fontSize: 13, color: AppColors.textSecondary)),
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: AppColors.surfaceAlt,
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: AppColors.border, width: 1),
              ),
              child: SelectableText(newPwd,
                  style: AppTheme.mono(fontSize: 14, fontWeight: FontWeight.w800, color: AppColors.warning)),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: newPwd));
              _snack('Copié dans le presse-papiers', ok: true);
            },
            child: const Text('Copier'),
          ),
          FilledButton(onPressed: () => Navigator.pop(ctx), child: const Text('Fermer')),
        ],
      ),
    );
  }

  Future<void> _viewPayments(AdminUser user) async {
    final payments = await ref.read(adminUsersProvider.notifier).fetchUserPayments(user.id);
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: AppColors.bg,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
      builder: (_) => _PaymentsSheet(user: user, payments: payments),
    );
  }

  Future<void> _toggleRole(AdminUser user) async {
    final newRole = user.isAdmin ? 'user' : 'admin';
    if (!await _confirm(
      'Changer le rôle',
      '${user.name.isEmpty ? user.email : user.name} passera de "${user.role}" à "$newRole".',
    )) return;
    final ok = await ref.read(adminUsersProvider.notifier).changeRole(user.id, newRole);
    _snack(ok ? 'Rôle mis à jour' : 'Échec', ok: ok, err: !ok);
  }

  Future<void> _toggleActive(AdminUser user) async {
    final activate = !user.active;
    if (!await _confirm(
      activate ? 'Réactiver' : 'Suspendre',
      '${activate ? 'Réactiver' : 'Suspendre'} ${user.name.isEmpty ? user.email : user.name} ?',
      destructive: !activate,
    )) return;
    final ok = await ref.read(adminUsersProvider.notifier).setActive(user.id, activate);
    _snack(ok ? 'Statut mis à jour' : 'Échec', ok: ok, err: !ok);
  }

  Future<void> _delete(AdminUser user) async {
    if (!await _confirm(
      'Supprimer le compte',
      'Supprimer définitivement ${user.name.isEmpty ? user.email : user.name} (${user.email}) ? Irréversible.',
      destructive: true,
    )) return;
    final ok = await ref.read(adminUsersProvider.notifier).deleteUser(user.id);
    _snack(ok ? 'Compte supprimé' : 'Échec', ok: ok, err: !ok);
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(adminUsersProvider);
    final users = _apply(state.users);

    return Scaffold(
      appBar: AppBar(
        title: Text('Administration', style: AppTheme.heading(fontSize: 15, letterSpacing: -0.2)),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded, size: 20),
            tooltip: 'Rafraîchir',
            onPressed: () => ref.read(adminUsersProvider.notifier).load(),
          ),
        ],
      ),
      body: RefreshIndicator(
        color: AppColors.primary,
        onRefresh: () => ref.read(adminUsersProvider.notifier).load(),
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: [
            if (state.stats != null) _StatsGrid(stats: state.stats!),
            if (state.stats != null) const SizedBox(height: 16),
            TextField(
              controller: _search,
              onChanged: (v) => setState(() => _query = v),
              style: GoogleFonts.manrope(color: AppColors.textPrimary, fontSize: 14),
              decoration: InputDecoration(
                hintText: 'Rechercher par nom ou email',
                prefixIcon: const Icon(Icons.search_rounded, size: 20),
                suffixIcon: _query.isEmpty
                    ? null
                    : IconButton(
                        icon: const Icon(Icons.clear_rounded, size: 18),
                        onPressed: () => setState(() { _search.clear(); _query = ''; }),
                      ),
                isDense: true,
              ),
            ),
            const SizedBox(height: 10),
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: [
                  for (final f in const [
                    ('all', 'Tous'),
                    ('admin', 'Admins'),
                    ('premium', 'Premium'),
                    ('trial', 'Essai actif'),
                    ('expired', 'Expirés'),
                    ('suspended', 'Suspendus'),
                  ])
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: _FilterChip(
                        label: f.$2,
                        selected: _filter == f.$1,
                        onTap: () => setState(() => _filter = f.$1),
                      ),
                    ),
                ],
              ),
            ),
            const SizedBox(height: 14),
            if (state.error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: Text(state.error!, style: GoogleFonts.manrope(color: AppColors.danger, fontSize: 12.5)),
              ),
            if (state.loading && state.users.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 40),
                child: Center(child: CircularProgressIndicator(color: AppColors.primary)),
              )
            else if (users.isEmpty)
              Container(
                padding: const EdgeInsets.all(28),
                decoration: BoxDecoration(
                  color: AppColors.surface,
                  borderRadius: BorderRadius.circular(16),
                  border: Border.all(color: AppColors.borderSoft, width: 1),
                ),
                child: Center(
                  child: Text('Aucun utilisateur ne correspond aux filtres.',
                      style: GoogleFonts.manrope(color: AppColors.textTertiary, fontSize: 13)),
                ),
              )
            else
              Column(
                children: [
                  for (final u in users) ...[
                    _UserCard(
                      user: u,
                      onGrantPremium: () => _grantPremium(u),
                      onRevokePremium: () => _revokePremium(u),
                      onResetTrial: () => _resetTrial(u),
                      onResetPassword: () => _resetPassword(u),
                      onViewPayments: () => _viewPayments(u),
                      onToggleRole: () => _toggleRole(u),
                      onToggleActive: () => _toggleActive(u),
                      onDelete: () => _delete(u),
                    ),
                    const SizedBox(height: 10),
                  ],
                ],
              ),
          ],
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Widgets
// ---------------------------------------------------------------------------

class _StatsGrid extends StatelessWidget {
  const _StatsGrid({required this.stats});
  final AdminStats stats;

  @override
  Widget build(BuildContext context) {
    final cards = <_StatCard>[
      _StatCard(label: 'Utilisateurs', value: '${stats.usersTotal}', sub: '${stats.adminsTotal} admin(s)', color: AppColors.primarySoft),
      _StatCard(label: 'Actifs', value: '${stats.usersActive}', sub: '${stats.usersSuspended} suspendus', color: AppColors.success),
      _StatCard(label: 'Essais', value: '${stats.trialActive}', sub: '7 jours gratuits', color: AppColors.primarySoft),
      _StatCard(label: 'Premium', value: '${stats.premiumActive}', sub: '${stats.paymentsPaid} paiements', color: AppColors.warning),
      _StatCard(label: 'CA total', value: _fmt(stats.revenueXofTotal), sub: 'XOF cumulés', color: AppColors.warning),
      _StatCard(label: 'CA 30j', value: _fmt(stats.revenueXof30d), sub: 'XOF glissants', color: AppColors.warning),
    ];
    return GridView.count(
      crossAxisCount: 2,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      mainAxisSpacing: 10,
      crossAxisSpacing: 10,
      childAspectRatio: 1.9,
      children: cards,
    );
  }

  static String _fmt(int n) {
    final s = n.toString();
    final buffer = StringBuffer();
    for (int i = 0; i < s.length; i++) {
      if (i > 0 && (s.length - i) % 3 == 0) buffer.write(' ');
      buffer.write(s[i]);
    }
    return buffer.toString();
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({required this.label, required this.value, required this.sub, required this.color});
  final String label, value, sub;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: AppColors.border, width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label.toUpperCase(), style: AppTheme.labelMicro().copyWith(fontSize: 10)),
          const SizedBox(height: 4),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: Text(value, style: AppTheme.mono(fontSize: 20, fontWeight: FontWeight.w800, color: color)),
          ),
          const Spacer(),
          Text(sub, style: GoogleFonts.manrope(fontSize: 10.5, color: AppColors.textTertiary, fontWeight: FontWeight.w600)),
        ],
      ),
    );
  }
}

class _FilterChip extends StatelessWidget {
  const _FilterChip({required this.label, required this.selected, required this.onTap});
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        decoration: BoxDecoration(
          color: selected ? AppColors.primary.withValues(alpha: 0.16) : AppColors.surfaceAlt,
          borderRadius: BorderRadius.circular(999),
          border: Border.all(
            color: selected ? AppColors.primary.withValues(alpha: 0.5) : AppColors.border,
            width: 1,
          ),
        ),
        child: Text(label,
            style: GoogleFonts.manrope(
              fontSize: 12,
              fontWeight: FontWeight.w800,
              color: selected ? AppColors.primarySoft : AppColors.textTertiary,
            )),
      ),
    );
  }
}

class _UserCard extends StatelessWidget {
  const _UserCard({
    required this.user,
    required this.onGrantPremium,
    required this.onRevokePremium,
    required this.onResetTrial,
    required this.onResetPassword,
    required this.onViewPayments,
    required this.onToggleRole,
    required this.onToggleActive,
    required this.onDelete,
  });

  final AdminUser user;
  final VoidCallback onGrantPremium, onRevokePremium, onResetTrial, onResetPassword;
  final VoidCallback onViewPayments, onToggleRole, onToggleActive, onDelete;

  String _fmtDate(DateTime? dt) {
    if (dt == null) return '—';
    final l = dt.toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(l.day)}/${two(l.month)}/${l.year}';
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: AppColors.border, width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  gradient: const LinearGradient(colors: [AppColors.primary, AppColors.primaryDeep]),
                  borderRadius: BorderRadius.circular(12),
                ),
                alignment: Alignment.center,
                child: Text(
                  (user.name.isNotEmpty ? user.name : user.email)[0].toUpperCase(),
                  style: GoogleFonts.manrope(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w800),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(user.name.isEmpty ? '(sans nom)' : user.name,
                        style: GoogleFonts.manrope(fontSize: 14, fontWeight: FontWeight.w800, color: AppColors.textPrimary)),
                    const SizedBox(height: 2),
                    Text(user.email,
                        style: GoogleFonts.manrope(fontSize: 12, color: AppColors.textTertiary),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                  ],
                ),
              ),
              PopupMenuButton<String>(
                icon: const Icon(Icons.more_vert_rounded, color: AppColors.textTertiary, size: 20),
                color: AppColors.surfaceHigh,
                itemBuilder: (_) => [
                  PopupMenuItem(value: 'role', child: Row(children: [
                    Icon(user.isAdmin ? Icons.person_outline : Icons.admin_panel_settings_outlined, size: 18, color: AppColors.textSecondary),
                    const SizedBox(width: 10),
                    Text(user.isAdmin ? 'Rétrograder en user' : 'Promouvoir admin', style: GoogleFonts.manrope(fontSize: 13)),
                  ])),
                  PopupMenuItem(value: 'trial', child: Row(children: [
                    const Icon(Icons.replay_rounded, size: 18, color: AppColors.textSecondary),
                    const SizedBox(width: 10),
                    Text('Redémarrer l\'essai 7j', style: GoogleFonts.manrope(fontSize: 13)),
                  ])),
                  if (user.isPremiumActive)
                    PopupMenuItem(value: 'revoke', child: Row(children: [
                      const Icon(Icons.remove_circle_outline, size: 18, color: AppColors.warning),
                      const SizedBox(width: 10),
                      Text('Retirer le premium', style: GoogleFonts.manrope(fontSize: 13)),
                    ])),
                  PopupMenuItem(value: 'password', child: Row(children: [
                    const Icon(Icons.key_outlined, size: 18, color: AppColors.textSecondary),
                    const SizedBox(width: 10),
                    Text('Reset mot de passe', style: GoogleFonts.manrope(fontSize: 13)),
                  ])),
                  PopupMenuItem(value: 'payments', child: Row(children: [
                    const Icon(Icons.receipt_long_outlined, size: 18, color: AppColors.textSecondary),
                    const SizedBox(width: 10),
                    Text('Historique paiements', style: GoogleFonts.manrope(fontSize: 13)),
                  ])),
                  const PopupMenuDivider(),
                  PopupMenuItem(value: 'delete', child: Row(children: [
                    const Icon(Icons.delete_outline_rounded, size: 18, color: AppColors.danger),
                    const SizedBox(width: 10),
                    Text('Supprimer le compte', style: GoogleFonts.manrope(fontSize: 13, color: AppColors.danger)),
                  ])),
                ],
                onSelected: (v) {
                  switch (v) {
                    case 'role': onToggleRole(); break;
                    case 'trial': onResetTrial(); break;
                    case 'revoke': onRevokePremium(); break;
                    case 'password': onResetPassword(); break;
                    case 'payments': onViewPayments(); break;
                    case 'delete': onDelete(); break;
                  }
                },
              ),
            ],
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _badge(user.role, user.isAdmin ? AppColors.primarySoft : AppColors.textTertiary),
              _badge(user.active ? 'actif' : 'suspendu', user.active ? AppColors.success : AppColors.danger),
              if (user.isPremiumActive)
                _badge('★ premium · ${_fmtDate(user.subscriptionExpiresAt)}', AppColors.warning)
              else if (user.isTrialActive)
                _badge('essai · ${user.trialDaysLeft}j', AppColors.primarySoft)
              else if (user.subscriptionTier == 'premium')
                _badge('premium expiré', AppColors.danger)
              else
                _badge('free', AppColors.textTertiary),
            ],
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  icon: const Icon(Icons.workspace_premium_rounded, size: 16),
                  label: const Text('Offrir premium'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: AppColors.warning,
                    side: BorderSide(color: AppColors.warning.withValues(alpha: 0.35)),
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    textStyle: GoogleFonts.manrope(fontSize: 12, fontWeight: FontWeight.w800),
                  ),
                  onPressed: onGrantPremium,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: OutlinedButton.icon(
                  icon: Icon(user.active ? Icons.block_rounded : Icons.check_circle_outline, size: 16),
                  label: Text(user.active ? 'Suspendre' : 'Réactiver'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: user.active ? AppColors.danger : AppColors.success,
                    side: BorderSide(color: (user.active ? AppColors.danger : AppColors.success).withValues(alpha: 0.35)),
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    textStyle: GoogleFonts.manrope(fontSize: 12, fontWeight: FontWeight.w800),
                  ),
                  onPressed: onToggleActive,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _badge(String text, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(text,
          style: AppTheme.mono(fontSize: 10.5, fontWeight: FontWeight.w700, color: color)),
    );
  }
}

class _GrantPremiumDialog extends StatefulWidget {
  const _GrantPremiumDialog({required this.userName});
  final String userName;

  @override
  State<_GrantPremiumDialog> createState() => _GrantPremiumDialogState();
}

class _GrantPremiumDialogState extends State<_GrantPremiumDialog> {
  int _selected = 30;
  final TextEditingController _custom = TextEditingController();

  @override
  void dispose() {
    _custom.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: AppColors.surface,
      title: Row(
        children: [
          const Icon(Icons.workspace_premium_rounded, color: AppColors.warning),
          const SizedBox(width: 8),
          Text('Offrir premium', style: AppTheme.heading(fontSize: 16)),
        ],
      ),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Durée à ajouter au compte de ${widget.userName} :',
                style: GoogleFonts.manrope(fontSize: 13, color: AppColors.textSecondary)),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final n in const [7, 30, 90, 365])
                  ChoiceChip(
                    label: Text('${n}j${n == 365 ? ' (annuel)' : n == 30 ? ' (mensuel)' : ''}'),
                    selected: _selected == n,
                    onSelected: (_) => setState(() { _selected = n; _custom.clear(); }),
                    selectedColor: AppColors.warning.withValues(alpha: 0.24),
                    labelStyle: GoogleFonts.manrope(
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      color: _selected == n ? AppColors.warning : AppColors.textSecondary,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 14),
            TextField(
              controller: _custom,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'Durée personnalisée (jours)',
                isDense: true,
              ),
              onChanged: (v) {
                final n = int.tryParse(v);
                if (n != null && n > 0) setState(() => _selected = n);
              },
            ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Annuler')),
        FilledButton(
          onPressed: () => Navigator.pop(context, _selected),
          style: FilledButton.styleFrom(backgroundColor: AppColors.warning, foregroundColor: Colors.black),
          child: Text('Activer $_selected jours'),
        ),
      ],
    );
  }
}

class _PaymentsSheet extends StatelessWidget {
  const _PaymentsSheet({required this.user, required this.payments});
  final AdminUser user;
  final List<AdminPayment> payments;

  String _fmtDate(DateTime dt) {
    final l = dt.toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(l.day)}/${two(l.month)}/${l.year} ${two(l.hour)}:${two(l.minute)}';
  }

  Color _statusColor(String s) {
    switch (s) {
      case 'paid': return AppColors.success;
      case 'pending': return AppColors.warning;
      default: return AppColors.danger;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
      child: SafeArea(
        top: false,
        child: Container(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
          constraints: BoxConstraints(maxHeight: MediaQuery.of(context).size.height * 0.7),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: [
              Center(
                child: Container(
                  width: 42, height: 4,
                  margin: const EdgeInsets.only(bottom: 12),
                  decoration: BoxDecoration(color: Colors.white.withValues(alpha: 0.14), borderRadius: BorderRadius.circular(999)),
                ),
              ),
              Text('Paiements — ${user.name.isEmpty ? user.email : user.name}',
                  style: AppTheme.heading(fontSize: 16, letterSpacing: -0.2)),
              const SizedBox(height: 14),
              if (payments.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 24),
                  child: Center(
                    child: Text('Aucun paiement pour cet utilisateur.',
                        style: GoogleFonts.manrope(color: AppColors.textTertiary, fontSize: 13)),
                  ),
                )
              else
                Flexible(
                  child: ListView.separated(
                    shrinkWrap: true,
                    itemCount: payments.length,
                    separatorBuilder: (_, __) => const SizedBox(height: 8),
                    itemBuilder: (_, i) {
                      final p = payments[i];
                      final color = _statusColor(p.status);
                      return Container(
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: AppColors.surfaceAlt,
                          borderRadius: BorderRadius.circular(12),
                          border: Border.all(color: AppColors.border, width: 1),
                        ),
                        child: Row(
                          children: [
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(p.plan, style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w800, color: AppColors.textPrimary)),
                                  const SizedBox(height: 2),
                                  Text('${p.provider} · ${_fmtDate(p.createdAt)}',
                                      style: GoogleFonts.manrope(fontSize: 11, color: AppColors.textTertiary)),
                                ],
                              ),
                            ),
                            Text('${p.amountXof} XOF',
                                style: AppTheme.mono(fontSize: 13, fontWeight: FontWeight.w800, color: AppColors.warning)),
                            const SizedBox(width: 10),
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                              decoration: BoxDecoration(
                                color: color.withValues(alpha: 0.14),
                                borderRadius: BorderRadius.circular(999),
                              ),
                              child: Text(p.status,
                                  style: AppTheme.mono(fontSize: 10.5, fontWeight: FontWeight.w800, color: color)),
                            ),
                          ],
                        ),
                      );
                    },
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

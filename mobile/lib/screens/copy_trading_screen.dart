import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../providers/auth_provider.dart';
import '../providers/copy_provider.dart';
import '../services/copy_service.dart';
import '../services/jwt_utils.dart';
import '../theme/app_theme.dart';
import '../widgets/brand_logo.dart';
import 'login_screen.dart';
import 'premium_screen.dart';

// Bornes du contrat d'API (POST / PATCH /copy/follow).
const double _kMultMin = 0.1;
const double _kMultMax = 10;
const double _kStakeMin = 0.35;
const double _kStakeMax = 1000;
const double _kSlMin = 1;
const double _kSlMax = 100000;

/// Écran Copy Trading : suivre un trader maître et consulter les trades copiés.
class CopyTradingScreen extends ConsumerStatefulWidget {
  const CopyTradingScreen({super.key});

  @override
  ConsumerState<CopyTradingScreen> createState() => _CopyTradingScreenState();
}

class _CopyTradingScreenState extends ConsumerState<CopyTradingScreen> {
  Timer? _poll;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    // Rafraîchissement discret (PnL du jour, trades copiés) tant qu'une copie est en cours.
    _poll = Timer.periodic(const Duration(seconds: 20), (_) {
      if (!mounted) return;
      if (ref.read(copyProvider).following != null) {
        ref.read(copyProvider.notifier).refresh(silent: true);
      }
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _refresh() => ref.read(copyProvider.notifier).refresh();

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

  Future<bool> _confirm(
    String title,
    String message, {
    String confirmLabel = 'Confirmer',
    bool destructive = false,
  }) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppColors.surface,
        title: Text(title, style: AppTheme.heading(fontSize: 16)),
        content: Text(message,
            style: GoogleFonts.manrope(fontSize: 13.5, color: AppColors.textSecondary, height: 1.45)),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Annuler')),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: destructive ? FilledButton.styleFrom(backgroundColor: AppColors.danger) : null,
            child: Text(confirmLabel),
          ),
        ],
      ),
    );
    return ok ?? false;
  }

  void _openPremium([String? reason]) {
    if (reason != null && reason.isNotEmpty) _snack(reason);
    Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => const PremiumScreen()));
  }

  void _openLogin() {
    // LoginScreen remplace sa route par le tableau de bord une fois connecté.
    Navigator.of(context).pushReplacement(
      MaterialPageRoute<void>(builder: (_) => const LoginScreen()),
    );
  }

  Future<void> _openFollowSheet(MasterInfo master) async {
    final result = await showModalBottomSheet<_SheetResult>(
      context: context,
      backgroundColor: AppColors.bg,
      isScrollControlled: true,
      useSafeArea: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
      ),
      builder: (_) => _FollowSheet(master: master),
    );
    _handleSheetResult(result);
  }

  Future<void> _openEditSheet(FollowOut follow) async {
    final result = await showModalBottomSheet<_SheetResult>(
      context: context,
      backgroundColor: AppColors.bg,
      isScrollControlled: true,
      useSafeArea: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
      ),
      builder: (_) => _EditFollowSheet(follow: follow),
    );
    _handleSheetResult(result);
  }

  void _handleSheetResult(_SheetResult? result) {
    if (!mounted || result == null) return;
    switch (result.outcome) {
      case _Outcome.saved:
        _snack(result.message, ok: true);
      case _Outcome.premium:
        _openPremium(result.message);
      case _Outcome.stale:
        _snack(result.message, err: true);
        _refresh();
    }
  }

  Future<void> _unfollow(FollowOut follow) async {
    final ok = await _confirm(
      'Arrêter de copier',
      'Vous ne copierez plus les positions de ${follow.masterName}. '
          'Le token Deriv enregistré pour la copie sera supprimé du serveur.',
      confirmLabel: 'Arrêter',
      destructive: true,
    );
    if (!ok || !mounted) return;
    setState(() => _busy = true);
    try {
      await ref.read(copyProvider.notifier).unfollow();
      _snack('Copie de ${follow.masterName} arrêtée', ok: true);
    } on CopyServiceException catch (e) {
      _snack(e.userMessage(notFound: 'Aucune copie en cours.'), err: true);
    } catch (_) {
      _snack('Échec de l\'arrêt de la copie, réessayez.', err: true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final jwt = ref.watch(jwtProvider);
    final state = ref.watch(copyProvider);
    final bool reloading = state.loading && state.me != null;

    return Scaffold(
      appBar: AppBar(
        title: Text('Copy trading', style: AppTheme.heading(fontSize: 15, letterSpacing: -0.2)),
        actions: [
          if (jwt != null && jwt.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: reloading
                  ? const Padding(
                      padding: EdgeInsets.all(14),
                      child: SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.primary),
                      ),
                    )
                  : IconButton(
                      tooltip: 'Actualiser',
                      onPressed: _refresh,
                      icon: const Icon(Icons.refresh_rounded, size: 20),
                    ),
            ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: _refresh,
          color: AppColors.primary,
          backgroundColor: AppColors.surface,
          child: ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
            children: _content(jwt, state),
          ),
        ),
      ),
    );
  }

  List<Widget> _content(String? jwt, CopyState state) {
    if (jwt == null || jwt.isEmpty) {
      return [
        const SizedBox(height: 8),
        _NoticeCard(
          icon: Icons.person_outline,
          color: AppColors.warning,
          title: 'Connexion requise',
          message: 'Connectez-vous à votre compte pour copier un trader.',
          cta: 'Se connecter',
          onTap: _openLogin,
        ),
      ];
    }

    final me = state.me;
    if (me == null) {
      if (state.error != null && !state.loading) {
        return [
          const SizedBox(height: 8),
          _NoticeCard(
            icon: Icons.cloud_off_rounded,
            color: AppColors.danger,
            title: 'Chargement impossible',
            message: state.error!,
            cta: 'Réessayer',
            onTap: _refresh,
          ),
        ];
      }
      return const [
        Padding(
          padding: EdgeInsets.symmetric(vertical: 64),
          child: Center(child: CircularProgressIndicator(color: AppColors.primary)),
        ),
      ];
    }

    if (!me.enabled) {
      return const [
        SizedBox(height: 8),
        _NoticeCard(
          icon: Icons.hourglass_empty_rounded,
          color: AppColors.textTertiary,
          title: 'Bientôt disponible',
          message: 'Le copy trading n\'est pas encore activé sur le serveur.',
        ),
      ];
    }

    final following = me.following;
    return [
      if (me.isMaster) ...[
        _MasterBanner(followers: me.followersCount),
        const SizedBox(height: 14),
      ],
      if (state.error != null) ...[
        _ErrorStrip(message: state.error!, onRetry: _refresh),
        const SizedBox(height: 14),
      ],
      if (following != null)
        ..._followingSection(following, state.trades)
      else
        ..._mastersSection(state.masters, _userIdOf(jwt)),
    ];
  }

  List<Widget> _followingSection(FollowOut follow, List<CopiedTrade> trades) => [
        _FollowingCard(
          follow: follow,
          busy: _busy,
          onEdit: () => _openEditSheet(follow),
          onStop: () => _unfollow(follow),
        ),
        const SizedBox(height: 20),
        Row(
          children: [
            Text('Trades copiés', style: AppTheme.heading(fontSize: 13, letterSpacing: 0.2)),
            const Spacer(),
            if (trades.isNotEmpty)
              Text('${trades.length}',
                  style: AppTheme.mono(fontSize: 11.5, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
          ],
        ),
        const SizedBox(height: 12),
        _CopiedTradesList(trades: trades, currency: follow.accountCurrency),
      ];

  List<Widget> _mastersSection(List<MasterInfo> masters, int? myId) => [
        const _IntroCard(),
        const SizedBox(height: 20),
        Text('Traders maîtres', style: AppTheme.heading(fontSize: 13, letterSpacing: 0.2)),
        const SizedBox(height: 12),
        if (masters.isEmpty)
          const _EmptyCard(
            title: 'Aucun trader maître disponible',
            message: 'Revenez plus tard : la liste est tenue à jour par l\'équipe.',
          )
        else
          for (final m in masters) ...[
            _MasterCard(master: m, isSelf: m.masterId == myId, onCopy: () => _openFollowSheet(m)),
            const SizedBox(height: 10),
          ],
        const SizedBox(height: 8),
        Text(
          'Les performances passées ne préjugent pas des résultats futurs.',
          textAlign: TextAlign.center,
          style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.4),
        ),
      ];
}

// ---------------------------------------------------------------------------
// Bandeaux et cartes d'information
// ---------------------------------------------------------------------------

class _NoticeCard extends StatelessWidget {
  const _NoticeCard({
    required this.icon,
    required this.color,
    required this.title,
    required this.message,
    this.cta,
    this.onTap,
  });

  final IconData icon;
  final Color color;
  final String title;
  final String message;
  final String? cta;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: AppTheme.cardGradient(),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: color.withValues(alpha: 0.14),
              borderRadius: BorderRadius.circular(12),
            ),
            alignment: Alignment.center,
            child: Icon(icon, color: color, size: 20),
          ),
          const SizedBox(height: 16),
          Text(title, style: AppTheme.heading(fontSize: 18)),
          const SizedBox(height: 8),
          Text(message, style: GoogleFonts.manrope(fontSize: 13.5, height: 1.5, color: AppColors.textSecondary)),
          if (cta != null && onTap != null) ...[
            const SizedBox(height: 16),
            FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: AppColors.primary.withValues(alpha: 0.16),
                foregroundColor: AppColors.primarySoft,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadii.md)),
              ),
              onPressed: onTap,
              child: Text(cta!, style: GoogleFonts.manrope(fontSize: 13.5, fontWeight: FontWeight.w800)),
            ),
          ],
        ],
      ),
    );
  }
}

class _ErrorStrip extends StatelessWidget {
  const _ErrorStrip({required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 6, 6, 6),
      decoration: BoxDecoration(
        color: AppColors.danger.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md),
        border: Border.all(color: AppColors.danger.withValues(alpha: 0.35), width: 1),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline_rounded, size: 18, color: AppColors.danger),
          const SizedBox(width: 10),
          Expanded(
            child: Text(message,
                style: GoogleFonts.manrope(fontSize: 12.5, color: AppColors.textPrimary, height: 1.35)),
          ),
          TextButton(
            onPressed: onRetry,
            child: Text('Réessayer',
                style: GoogleFonts.manrope(fontSize: 12, fontWeight: FontWeight.w800, color: AppColors.danger)),
          ),
        ],
      ),
    );
  }
}

/// Bandeau affiché quand l'utilisateur est lui-même trader maître.
class _MasterBanner extends StatelessWidget {
  const _MasterBanner({required this.followers});
  final int followers;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: AppColors.primary.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md + 2),
        border: Border.all(color: AppColors.primary.withValues(alpha: 0.35), width: 1),
      ),
      child: Row(
        children: [
          const Icon(Icons.military_tech_rounded, size: 20, color: AppColors.primarySoft),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('Vous êtes trader maître · ${_followersLabel(followers)}',
                    style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w800, color: AppColors.textPrimary)),
                const SizedBox(height: 2),
                Text('Les positions ouvertes par votre robot sont répliquées chez vos abonnés.',
                    style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.35)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _IntroCard extends StatelessWidget {
  const _IntroCard();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: AppTheme.cardGradient(),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: AppColors.primary.withValues(alpha: 0.14),
              borderRadius: BorderRadius.circular(12),
            ),
            alignment: Alignment.center,
            child: const Icon(Icons.copy_all_rounded, color: AppColors.primarySoft, size: 19),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Copiez un trader', style: AppTheme.heading(fontSize: 16)),
                const SizedBox(height: 6),
                Text(
                  'Chaque position ouverte par le trader via le robot est reproduite sur votre compte Deriv, '
                  'ajustée par votre multiplicateur et limitée par vos garde-fous.',
                  style: GoogleFonts.manrope(fontSize: 12.5, height: 1.5, color: AppColors.textSecondary),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyCard extends StatelessWidget {
  const _EmptyCard({required this.title, required this.message});
  final String title;
  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(24),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(AppRadii.lg),
        border: Border.all(color: Colors.white.withValues(alpha: 0.12), width: 1),
      ),
      child: Column(
        children: [
          Text(title,
              textAlign: TextAlign.center,
              style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textSecondary)),
          const SizedBox(height: 6),
          Text(message,
              textAlign: TextAlign.center,
              style: GoogleFonts.manrope(fontSize: 12, color: AppColors.textTertiary, height: 1.4)),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Copie en cours
// ---------------------------------------------------------------------------

class _FollowingCard extends StatelessWidget {
  const _FollowingCard({
    required this.follow,
    required this.busy,
    required this.onEdit,
    required this.onStop,
  });

  final FollowOut follow;
  final bool busy;
  final VoidCallback onEdit;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    final f = follow;
    final String cur = f.accountCurrency;
    final Color pnlColor = f.todayPnl >= 0 ? AppColors.success : AppColors.danger;
    final (String pill, Color pillColor, String statusText, IconData statusIcon) = _followStatus(f);
    final double loss = f.todayPnl < 0 ? -f.todayPnl : 0;

    return Container(
      padding: const EdgeInsets.all(20),
      decoration: AppTheme.cardGradient(),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              _Avatar(name: f.masterName),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('COPIE EN COURS',
                        style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.9)),
                    const SizedBox(height: 4),
                    Text('Vous copiez ${f.masterName}',
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: AppTheme.heading(fontSize: 16, letterSpacing: -0.3)),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              StatusPill(label: pill, color: pillColor),
            ],
          ),
          const SizedBox(height: 18),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('PNL DU JOUR', style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.9)),
                    const SizedBox(height: 8),
                    // Réduit plutôt que de déborder (gros montant, écran étroit).
                    FittedBox(
                      fit: BoxFit.scaleDown,
                      alignment: Alignment.centerLeft,
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.baseline,
                        textBaseline: TextBaseline.alphabetic,
                        children: [
                          Text(_signed(f.todayPnl),
                              style: AppTheme.mono(
                                  fontSize: 30, fontWeight: FontWeight.w700, letterSpacing: -1.2, color: pnlColor)),
                          const SizedBox(width: 8),
                          Text(cur,
                              style: GoogleFonts.manrope(
                                  fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Text('COMPTE', style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.9)),
                  const SizedBox(height: 8),
                  _AccountChip(real: f.isReal, currency: cur),
                ],
              ),
            ],
          ),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.035),
              borderRadius: BorderRadius.circular(13),
            ),
            child: Column(
              children: [
                _SettingRow(label: 'Multiplicateur', value: _fmtMult(f.multiplier)),
                const SizedBox(height: 9),
                _SettingRow(label: 'Mise max par trade', value: _withCur(_money(f.maxStake), cur)),
                const SizedBox(height: 9),
                _SettingRow(label: 'Stop loss journalier', value: _withCur(_money(f.dailyStopLoss), cur)),
              ],
            ),
          ),
          const SizedBox(height: 14),
          _LossGauge(loss: loss, cap: f.dailyStopLoss, currency: cur),
          const SizedBox(height: 14),
          _StatusLine(icon: statusIcon, color: pillColor, text: statusText),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                flex: 2,
                child: _ActionButton(
                  label: 'Modifier',
                  icon: Icons.tune_rounded,
                  color: AppColors.primarySoft,
                  onPressed: busy ? null : onEdit,
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                flex: 3,
                child: _ActionButton(
                  label: 'Arrêter de copier',
                  icon: Icons.stop_circle_outlined,
                  color: AppColors.danger,
                  busy: busy,
                  onPressed: busy ? null : onStop,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _AccountChip extends StatelessWidget {
  const _AccountChip({required this.real, required this.currency});
  final bool real;
  final String currency;

  @override
  Widget build(BuildContext context) {
    final Color color = real ? AppColors.warning : AppColors.success;
    final String label = '${real ? 'Réel' : 'Démo'}${currency.isEmpty ? '' : ' · $currency'}';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(9),
      ),
      child: Text(label, style: AppTheme.mono(fontSize: 11, fontWeight: FontWeight.w700, color: color)),
    );
  }
}

class _SettingRow extends StatelessWidget {
  const _SettingRow({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Text(label,
              style: GoogleFonts.manrope(fontSize: 12.5, fontWeight: FontWeight.w600, color: AppColors.textSecondary)),
        ),
        Text(value, style: AppTheme.mono(fontSize: 12.5, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
      ],
    );
  }
}

/// Jauge de la perte du jour rapportée au stop loss journalier.
class _LossGauge extends StatelessWidget {
  const _LossGauge({required this.loss, required this.cap, required this.currency});
  final double loss;
  final double cap;
  final String currency;

  @override
  Widget build(BuildContext context) {
    final double pct = cap <= 0 ? 0.0 : (loss / cap).clamp(0.0, 1.0);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text('Perte du jour',
                style: GoogleFonts.manrope(fontSize: 11.5, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
            const SizedBox(width: 10),
            // Montants réduits plutôt que tronqués (stop loss journalier jusqu'à 100 000).
            Expanded(
              child: FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerRight,
                child: Text('${_money(loss)} / ${_withCur(_money(cap), currency)}',
                    style: AppTheme.mono(fontSize: 11.5, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
              ),
            ),
          ],
        ),
        const SizedBox(height: 7),
        Container(
          height: 6,
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.06),
            borderRadius: BorderRadius.circular(999),
          ),
          child: FractionallySizedBox(
            widthFactor: pct,
            alignment: Alignment.centerLeft,
            child: Container(
              decoration: BoxDecoration(color: AppColors.danger, borderRadius: BorderRadius.circular(999)),
            ),
          ),
        ),
      ],
    );
  }
}

class _StatusLine extends StatelessWidget {
  const _StatusLine({required this.icon, required this.color, required this.text});
  final IconData icon;
  final Color color;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md),
        border: Border.all(color: color.withValues(alpha: 0.3), width: 1),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 18, color: color),
          const SizedBox(width: 10),
          Expanded(
            child: Text(text,
                style: GoogleFonts.manrope(fontSize: 12.5, color: AppColors.textPrimary, height: 1.4)),
          ),
        ],
      ),
    );
  }
}

class _ActionButton extends StatelessWidget {
  const _ActionButton({
    required this.label,
    required this.icon,
    required this.color,
    required this.onPressed,
    this.busy = false,
  });

  final String label;
  final IconData icon;
  final Color color;
  final VoidCallback? onPressed;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 46,
      child: TextButton(
        style: TextButton.styleFrom(
          backgroundColor: color.withValues(alpha: 0.12),
          foregroundColor: color,
          disabledForegroundColor: color.withValues(alpha: 0.5),
          padding: const EdgeInsets.symmetric(horizontal: 10),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(AppRadii.md),
            side: BorderSide(color: color.withValues(alpha: 0.35), width: 1),
          ),
        ),
        onPressed: onPressed,
        child: busy
            ? SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2, color: color),
              )
            : Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(icon, size: 17),
                  const SizedBox(width: 6),
                  Flexible(
                    child: Text(label,
                        overflow: TextOverflow.ellipsis,
                        style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w800)),
                  ),
                ],
              ),
      ),
    );
  }
}

class _CopiedTradesList extends StatelessWidget {
  const _CopiedTradesList({required this.trades, required this.currency});
  final List<CopiedTrade> trades;
  final String currency;

  @override
  Widget build(BuildContext context) {
    if (trades.isEmpty) {
      return const _EmptyCard(
        title: 'Aucun trade copié pour le moment',
        message: 'Les positions du trader apparaîtront ici dès qu\'il en ouvrira une.',
      );
    }
    return Column(
      children: [
        for (final t in trades) ...[
          _CopiedTradeRow(trade: t, currency: currency),
          const SizedBox(height: 8),
        ],
      ],
    );
  }
}

class _CopiedTradeRow extends StatelessWidget {
  const _CopiedTradeRow({required this.trade, required this.currency});
  final CopiedTrade trade;
  final String currency;

  @override
  Widget build(BuildContext context) {
    final t = trade;
    final (String label, Color color, IconData icon) = _tradeVisual(t.status);
    final String dir = _directionLabel(t.contractType);
    final String time = _fmtDateTime(t.createdAt);
    final String? reason =
        (t.status == 'skipped' || t.status == 'failed') && t.reason != null ? _tradeReasonLabel(t.reason!) : null;
    final double? profit = t.profit;
    final Color profitColor = profit == null
        ? AppColors.textTertiary
        : (profit >= 0 ? AppColors.success : AppColors.danger);

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: AppTheme.card(radius: AppRadii.md + 2, border: AppColors.borderSoft),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 34,
            height: 34,
            decoration: BoxDecoration(color: color.withValues(alpha: 0.13), borderRadius: BorderRadius.circular(11)),
            alignment: Alignment.center,
            child: Icon(icon, size: 17, color: color),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(t.symbol.isEmpty ? dir : '$dir · ${t.symbol}',
                    style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                const SizedBox(height: 3),
                Text(
                  [if (time.isNotEmpty) time, 'mise ${_withCur(_money(t.stake), currency)}'].join(' · '),
                  style: AppTheme.mono(fontSize: 10.5, color: AppColors.textTertiary),
                ),
                if (reason != null) ...[
                  const SizedBox(height: 4),
                  Text(reason,
                      style: GoogleFonts.manrope(
                        fontSize: 11.5,
                        height: 1.35,
                        color: t.status == 'failed' ? AppColors.danger : AppColors.textSecondary,
                      )),
                ],
              ],
            ),
          ),
          const SizedBox(width: 10),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(profit == null ? '—' : _signed(profit),
                  style: AppTheme.mono(fontSize: 14, fontWeight: FontWeight.w700, color: profitColor)),
              const SizedBox(height: 4),
              Text(label, style: AppTheme.labelMicro(color: color)),
            ],
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Liste des traders maîtres
// ---------------------------------------------------------------------------

class _MasterCard extends StatelessWidget {
  const _MasterCard({required this.master, required this.onCopy, this.isSelf = false});
  final MasterInfo master;
  final VoidCallback onCopy;

  /// Carte de l'utilisateur lui-même : le serveur refuse de se copier soi-même (400).
  final bool isSelf;

  @override
  Widget build(BuildContext context) {
    final m = master;
    final s = m.stats;
    final double? pnl = s.pnl;
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: AppTheme.card(radius: AppRadii.lg + 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              _Avatar(name: m.displayName),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(m.displayName,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: AppTheme.heading(fontSize: 14.5, letterSpacing: -0.2)),
                    const SizedBox(height: 3),
                    Row(
                      children: [
                        const Icon(Icons.people_alt_outlined, size: 13, color: AppColors.textTertiary),
                        const SizedBox(width: 4),
                        Flexible(
                          child: Text(_followersLabel(m.followers),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: GoogleFonts.manrope(
                                  fontSize: 11.5, fontWeight: FontWeight.w600, color: AppColors.textTertiary)),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 10),
              if (isSelf)
                const StatusPill(label: 'VOUS', color: AppColors.primarySoft)
              else
                FilledButton(
                  onPressed: onCopy,
                  style: FilledButton.styleFrom(
                    backgroundColor: AppColors.primary,
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    minimumSize: const Size(0, 38),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadii.sm)),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.copy_all_rounded, size: 16),
                      const SizedBox(width: 6),
                      Text('Copier', style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w800)),
                    ],
                  ),
                ),
            ],
          ),
          if (m.bio.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(m.bio,
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                style: GoogleFonts.manrope(fontSize: 12.5, color: AppColors.textSecondary, height: 1.45)),
          ],
          const SizedBox(height: 14),
          Text('PERFORMANCE · ${s.windowDays} JOURS', style: AppTheme.labelMicro()),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(child: _StatTile(label: 'TRADES', value: s.trades?.toString() ?? '—')),
              const SizedBox(width: 8),
              Expanded(child: _StatTile(label: 'RÉUSSITE', value: _fmtWinRate(s.winRatePercent))),
              const SizedBox(width: 8),
              Expanded(
                child: _StatTile(
                  label: 'PNL',
                  value: pnl == null ? '—' : _signed(pnl),
                  color: pnl == null ? AppColors.textPrimary : (pnl >= 0 ? AppColors.success : AppColors.danger),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _StatTile extends StatelessWidget {
  const _StatTile({required this.label, required this.value, this.color = AppColors.textPrimary});
  final String label;
  final String value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.035),
        borderRadius: BorderRadius.circular(13),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: AppTheme.labelMicro()),
          const SizedBox(height: 5),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: Text(value, style: AppTheme.mono(fontSize: 15, fontWeight: FontWeight.w700, color: color)),
          ),
        ],
      ),
    );
  }
}

class _Avatar extends StatelessWidget {
  const _Avatar({required this.name});
  final String name;

  @override
  Widget build(BuildContext context) {
    final String trimmed = name.trim();
    final String initial = trimmed.isEmpty ? '?' : trimmed.characters.first.toUpperCase();
    return Container(
      width: 40,
      height: 40,
      decoration: BoxDecoration(
        color: AppColors.primary.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(12),
      ),
      alignment: Alignment.center,
      child: Text(initial,
          style: GoogleFonts.manrope(fontSize: 16, fontWeight: FontWeight.w800, color: AppColors.primarySoft)),
    );
  }
}

// ---------------------------------------------------------------------------
// Feuilles : suivre un maître / modifier la copie
// ---------------------------------------------------------------------------

enum _Outcome { saved, premium, stale }

/// Résultat renvoyé par les feuilles à l'écran principal.
class _SheetResult {
  const _SheetResult(this.outcome, this.message);
  final _Outcome outcome;
  final String message;
}

class _FollowSheet extends ConsumerStatefulWidget {
  const _FollowSheet({required this.master});
  final MasterInfo master;

  @override
  ConsumerState<_FollowSheet> createState() => _FollowSheetState();
}

class _FollowSheetState extends ConsumerState<_FollowSheet> {
  final GlobalKey<FormState> _formKey = GlobalKey<FormState>();
  final TextEditingController _tokenCtrl = TextEditingController();
  final TextEditingController _multCtrl = TextEditingController(text: '1.0');
  final TextEditingController _stakeCtrl = TextEditingController(text: '10');
  final TextEditingController _slCtrl = TextEditingController(text: '20');
  String _accountType = 'demo';
  bool _obscure = true;
  bool _consent = false;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _tokenCtrl.dispose();
    _multCtrl.dispose();
    _stakeCtrl.dispose();
    _slCtrl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_busy || !_consent) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(copyProvider.notifier).follow(
            masterId: widget.master.masterId,
            apiToken: _tokenCtrl.text.trim(),
            accountType: _accountType,
            multiplier: _parseNum(_multCtrl.text) ?? 1.0,
            maxStake: _parseNum(_stakeCtrl.text) ?? 10,
            dailyStopLoss: _parseNum(_slCtrl.text) ?? 20,
            consent: _consent,
          );
      if (!mounted) return;
      Navigator.of(context).pop(_SheetResult(_Outcome.saved, 'Copie de ${widget.master.displayName} activée'));
    } on CopyServiceException catch (e) {
      if (!mounted) return;
      switch (e.statusCode) {
        case 402:
          Navigator.of(context).pop(_SheetResult(_Outcome.premium, e.userMessage()));
        case 404 || 409 || 503:
          Navigator.of(context).pop(_SheetResult(_Outcome.stale, e.userMessage()));
        default:
          setState(() {
            _busy = false;
            _error = e.userMessage();
          });
      }
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = 'Une erreur inattendue est survenue, réessayez.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final m = widget.master;
    final s = m.stats;
    final double? pnl = s.pnl;
    final String perf = 'Performance sur ${s.windowDays} j : ${s.trades?.toString() ?? '—'} trades · '
        'réussite ${_fmtWinRate(s.winRatePercent)} · PnL ${pnl == null ? '—' : _signed(pnl)}';

    return Form(
      key: _formKey,
      child: _SheetFrame(
        title: 'Copier ${m.displayName}',
        subtitle: perf,
        children: [
          const _FieldLabel('TOKEN API DERIV'),
          const SizedBox(height: 8),
          TextFormField(
            controller: _tokenCtrl,
            enabled: !_busy,
            obscureText: _obscure,
            autocorrect: false,
            enableSuggestions: false,
            keyboardType: TextInputType.visiblePassword,
            style: AppTheme.mono(fontSize: 14, color: AppColors.textPrimary, letterSpacing: 1),
            decoration: InputDecoration(
              hintText: 'Token Read + Trade',
              suffixIcon: TextButton(
                onPressed: () => setState(() => _obscure = !_obscure),
                child: Text(_obscure ? 'Voir' : 'Masquer',
                    style: GoogleFonts.manrope(color: AppColors.textTertiary, fontSize: 11, fontWeight: FontWeight.w700)),
              ),
            ),
            validator: (v) {
              final t = (v ?? '').trim();
              if (t.isEmpty) return 'Token requis';
              if (t.length < 8) return 'Token trop court (8 caractères minimum)';
              return null;
            },
          ),
          const SizedBox(height: 16),
          const _FieldLabel('COMPTE DERIV'),
          const SizedBox(height: 8),
          _AccountSegment(
            value: _accountType,
            enabled: !_busy,
            onChanged: (v) => setState(() => _accountType = v),
          ),
          if (_accountType == 'real') ...[
            const SizedBox(height: 8),
            Text('Compte réel : les ordres copiés engagent votre argent réel.',
                style: GoogleFonts.manrope(fontSize: 12, fontWeight: FontWeight.w700, color: AppColors.warning)),
          ],
          const SizedBox(height: 16),
          const _FieldLabel('RÉGLAGES DE COPIE'),
          const SizedBox(height: 6),
          Text(
            'Votre mise = mise du trader × multiplicateur, limitée à la mise max. '
            'La copie se met en pause jusqu\'au lendemain si la perte du jour atteint le stop loss journalier.',
            style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.45),
          ),
          const SizedBox(height: 12),
          _NumberField(
            controller: _multCtrl,
            label: 'Multiplicateur',
            min: _kMultMin,
            max: _kMultMax,
            suffix: '×',
            enabled: !_busy,
          ),
          const SizedBox(height: 12),
          _NumberField(
            controller: _stakeCtrl,
            label: 'Mise max par trade',
            min: _kStakeMin,
            max: _kStakeMax,
            enabled: !_busy,
          ),
          const SizedBox(height: 12),
          _NumberField(
            controller: _slCtrl,
            label: 'Stop loss journalier',
            min: _kSlMin,
            max: _kSlMax,
            enabled: !_busy,
          ),
          const SizedBox(height: 16),
          const _TokenWarning(),
          const SizedBox(height: 12),
          _ConsentRow(
            value: _consent,
            enabled: !_busy,
            onChanged: (v) => setState(() => _consent = v),
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            _InlineError(message: _error!),
          ],
          const SizedBox(height: 16),
          _SheetButton(
            label: 'Commencer la copie',
            busy: _busy,
            onPressed: _consent ? _submit : null,
          ),
        ],
      ),
    );
  }
}

class _EditFollowSheet extends ConsumerStatefulWidget {
  const _EditFollowSheet({required this.follow});
  final FollowOut follow;

  @override
  ConsumerState<_EditFollowSheet> createState() => _EditFollowSheetState();
}

class _EditFollowSheetState extends ConsumerState<_EditFollowSheet> {
  final GlobalKey<FormState> _formKey = GlobalKey<FormState>();
  late final TextEditingController _multCtrl = TextEditingController(text: _fmtDecimal(widget.follow.multiplier));
  late final TextEditingController _stakeCtrl = TextEditingController(text: _fmtPlain(widget.follow.maxStake));
  late final TextEditingController _slCtrl = TextEditingController(text: _fmtPlain(widget.follow.dailyStopLoss));
  late bool _active = widget.follow.active;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _multCtrl.dispose();
    _stakeCtrl.dispose();
    _slCtrl.dispose();
    super.dispose();
  }

  static bool _differs(double a, double b) => (a - b).abs() > 1e-9;

  Future<void> _submit() async {
    if (_busy) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    final f = widget.follow;
    final double mult = _parseNum(_multCtrl.text) ?? f.multiplier;
    final double stake = _parseNum(_stakeCtrl.text) ?? f.maxStake;
    final double sl = _parseNum(_slCtrl.text) ?? f.dailyStopLoss;
    final double? newMult = _differs(mult, f.multiplier) ? mult : null;
    final double? newStake = _differs(stake, f.maxStake) ? stake : null;
    final double? newSl = _differs(sl, f.dailyStopLoss) ? sl : null;
    final bool? newActive = _active != f.active ? _active : null;
    if (newMult == null && newStake == null && newSl == null && newActive == null) {
      Navigator.of(context).pop();
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(copyProvider.notifier).update(
            multiplier: newMult,
            maxStake: newStake,
            dailyStopLoss: newSl,
            active: newActive,
          );
      if (!mounted) return;
      Navigator.of(context).pop(const _SheetResult(_Outcome.saved, 'Réglages de copie enregistrés'));
    } on CopyServiceException catch (e) {
      if (!mounted) return;
      final String message = e.userMessage(notFound: 'Aucune copie en cours.');
      switch (e.statusCode) {
        case 402:
          Navigator.of(context).pop(_SheetResult(_Outcome.premium, message));
        case 404 || 503:
          Navigator.of(context).pop(_SheetResult(_Outcome.stale, message));
        default:
          setState(() {
            _busy = false;
            _error = message;
          });
      }
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = 'Une erreur inattendue est survenue, réessayez.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final f = widget.follow;
    final String cur = f.accountCurrency;
    return Form(
      key: _formKey,
      child: _SheetFrame(
        title: 'Modifier la copie',
        subtitle: 'Trader ${f.masterName} · compte ${f.isReal ? 'réel' : 'démo'}${cur.isEmpty ? '' : ' ($cur)'}',
        children: [
          _NumberField(
            controller: _multCtrl,
            label: 'Multiplicateur',
            min: _kMultMin,
            max: _kMultMax,
            suffix: '×',
            enabled: !_busy,
          ),
          const SizedBox(height: 12),
          _NumberField(
            controller: _stakeCtrl,
            label: 'Mise max par trade',
            min: _kStakeMin,
            max: _kStakeMax,
            suffix: cur.isEmpty ? null : cur,
            enabled: !_busy,
          ),
          const SizedBox(height: 12),
          _NumberField(
            controller: _slCtrl,
            label: 'Stop loss journalier',
            min: _kSlMin,
            max: _kSlMax,
            suffix: cur.isEmpty ? null : cur,
            enabled: !_busy,
          ),
          const SizedBox(height: 16),
          _ActiveSwitchRow(
            value: _active,
            enabled: !_busy,
            onChanged: (v) => setState(() => _active = v),
          ),
          if (f.pausedReason != null) ...[
            const SizedBox(height: 10),
            _StatusLine(
              icon: Icons.pause_circle_outline_rounded,
              color: AppColors.warning,
              text: _pausedReasonLabel(f.pausedReason!),
            ),
          ],
          if (_error != null) ...[
            const SizedBox(height: 12),
            _InlineError(message: _error!),
          ],
          const SizedBox(height: 16),
          _SheetButton(label: 'Enregistrer', busy: _busy, onPressed: _submit),
        ],
      ),
    );
  }
}

/// Cadre commun des feuilles : poignée, titre, contenu défilant au-dessus du clavier.
class _SheetFrame extends StatelessWidget {
  const _SheetFrame({required this.title, required this.children, this.subtitle});
  final String title;
  final String? subtitle;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: SafeArea(
        top: false,
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(22, 10, 22, 20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: [
              Center(
                child: Container(
                  width: 42,
                  height: 4,
                  margin: const EdgeInsets.only(bottom: 12),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.14),
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
              ),
              Text(title, style: AppTheme.heading(fontSize: 21, letterSpacing: -0.5)),
              if (subtitle != null && subtitle!.isNotEmpty) ...[
                const SizedBox(height: 6),
                Text(subtitle!,
                    style: GoogleFonts.manrope(fontSize: 12.5, color: AppColors.textTertiary, height: 1.45)),
              ],
              const SizedBox(height: 18),
              ...children,
            ],
          ),
        ),
      ),
    );
  }
}

class _FieldLabel extends StatelessWidget {
  const _FieldLabel(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 4),
      child: Text(text, style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.8)),
    );
  }
}

class _NumberField extends StatelessWidget {
  const _NumberField({
    required this.controller,
    required this.label,
    required this.min,
    required this.max,
    this.suffix,
    this.enabled = true,
  });

  final TextEditingController controller;
  final String label;
  final double min;
  final double max;
  final String? suffix;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final String range = 'Entre ${_fmtPlain(min)} et ${_fmtPlain(max)}';
    return TextFormField(
      controller: controller,
      enabled: enabled,
      keyboardType: const TextInputType.numberWithOptions(decimal: true),
      inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.,]'))],
      style: AppTheme.mono(fontSize: 14, fontWeight: FontWeight.w700, color: AppColors.textPrimary),
      decoration: InputDecoration(
        labelText: label,
        helperText: range,
        helperStyle: GoogleFonts.manrope(fontSize: 11, color: AppColors.textTertiary),
        suffixText: suffix,
        suffixStyle: AppTheme.mono(fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textTertiary),
      ),
      validator: (v) {
        final n = _parseNum(v ?? '');
        if (n == null) return 'Valeur invalide';
        if (n < min || n > max) return range;
        return null;
      },
    );
  }
}

/// Segment Démo / Réel (même rendu que le sélecteur du tableau de bord).
class _AccountSegment extends StatelessWidget {
  const _AccountSegment({required this.value, required this.onChanged, this.enabled = true});
  final String value;
  final ValueChanged<String> onChanged;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(
        color: AppColors.surfaceAlt,
        borderRadius: BorderRadius.circular(AppRadii.md + 2),
        border: Border.all(color: AppColors.border, width: 1),
      ),
      child: Row(
        children: [
          _seg('demo', 'Démo', AppColors.success),
          _seg('real', 'Réel', AppColors.warning),
        ],
      ),
    );
  }

  Widget _seg(String key, String label, Color activeColor) {
    final selected = value == key;
    return Expanded(
      child: InkWell(
        onTap: enabled ? () => onChanged(key) : null,
        borderRadius: BorderRadius.circular(AppRadii.md - 2),
        child: Container(
          height: 40,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected ? activeColor.withValues(alpha: 0.16) : Colors.transparent,
            borderRadius: BorderRadius.circular(AppRadii.md - 2),
            border: Border.all(
              color: selected ? activeColor.withValues(alpha: 0.5) : Colors.transparent,
              width: 1,
            ),
          ),
          child: Text(
            label,
            style: GoogleFonts.manrope(
              fontSize: 13.5,
              fontWeight: FontWeight.w800,
              color: selected ? activeColor : AppColors.textTertiary,
              letterSpacing: 0.4,
            ),
          ),
        ),
      ),
    );
  }
}

/// Avertissement sur le token confié au serveur.
class _TokenWarning extends StatelessWidget {
  const _TokenWarning();

  @override
  Widget build(BuildContext context) {
    final TextStyle base = GoogleFonts.manrope(fontSize: 12.5, height: 1.5, color: AppColors.textSecondary);
    final TextStyle strong = base.copyWith(fontWeight: FontWeight.w800, color: AppColors.textPrimary);
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: AppColors.warning.withValues(alpha: 0.07),
        borderRadius: BorderRadius.circular(AppRadii.md),
        border: Border.all(color: AppColors.warning.withValues(alpha: 0.3), width: 1),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 26,
            height: 26,
            decoration: BoxDecoration(
              color: AppColors.warning.withValues(alpha: 0.14),
              borderRadius: BorderRadius.circular(8),
            ),
            alignment: Alignment.center,
            child: const Icon(Icons.shield_outlined, size: 15, color: AppColors.warning),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text.rich(
              TextSpan(
                style: base,
                children: [
                  const TextSpan(text: 'Créez un token dédié avec '),
                  TextSpan(text: 'UNIQUEMENT les autorisations Read et Trade — jamais Payments', style: strong),
                  const TextSpan(
                    text: '. Le serveur conserve ce token chiffré pour passer des ordres sur votre compte '
                        'quand le trader copié ouvre une position. '
                        'Les performances passées ne préjugent pas des résultats futurs.',
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _ConsentRow extends StatelessWidget {
  const _ConsentRow({required this.value, required this.onChanged, this.enabled = true});
  final bool value;
  final ValueChanged<bool> onChanged;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    return MergeSemantics(
      child: InkWell(
        onTap: enabled ? () => onChanged(!value) : null,
        borderRadius: BorderRadius.circular(AppRadii.md),
        child: Container(
          padding: const EdgeInsets.fromLTRB(4, 4, 14, 4),
          decoration: BoxDecoration(
            color: value ? AppColors.primary.withValues(alpha: 0.10) : Colors.white.withValues(alpha: 0.025),
            borderRadius: BorderRadius.circular(AppRadii.md),
            border: Border.all(
              color: value ? AppColors.primary.withValues(alpha: 0.45) : AppColors.borderSoft,
              width: 1,
            ),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Checkbox(
                value: value,
                onChanged: enabled ? (v) => onChanged(v ?? false) : null,
                activeColor: AppColors.primary,
                side: const BorderSide(color: AppColors.textTertiary, width: 1.5),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(5)),
              ),
              const SizedBox(width: 2),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  child: Text(
                    'J\'autorise l\'application à passer automatiquement des ordres sur mon compte Deriv '
                    'en copiant ce trader.',
                    style: GoogleFonts.manrope(
                        fontSize: 12.5, fontWeight: FontWeight.w600, color: AppColors.textPrimary, height: 1.45),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ActiveSwitchRow extends StatelessWidget {
  const _ActiveSwitchRow({required this.value, required this.onChanged, this.enabled = true});
  final bool value;
  final ValueChanged<bool> onChanged;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
      decoration: AppTheme.card(radius: AppRadii.md, border: AppColors.borderSoft),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Copie active',
                    style: GoogleFonts.manrope(fontSize: 13.5, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                const SizedBox(height: 3),
                Text('Désactivez pour suspendre la copie sans perdre vos réglages.',
                    style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.35)),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Switch(
            value: value,
            onChanged: enabled ? onChanged : null,
            thumbColor: WidgetStateProperty.resolveWith(
              (states) => states.contains(WidgetState.selected) ? AppColors.success : AppColors.textTertiary,
            ),
            trackColor: WidgetStateProperty.resolveWith(
              (states) => states.contains(WidgetState.selected)
                  ? AppColors.success.withValues(alpha: 0.35)
                  : AppColors.surfaceHigh,
            ),
            trackOutlineColor: const WidgetStatePropertyAll(AppColors.border),
          ),
        ],
      ),
    );
  }
}

class _InlineError extends StatelessWidget {
  const _InlineError({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppColors.danger.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md),
        border: Border.all(color: AppColors.danger.withValues(alpha: 0.35), width: 1),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.error_outline_rounded, size: 18, color: AppColors.danger),
          const SizedBox(width: 10),
          Expanded(
            child: Text(message,
                style: GoogleFonts.manrope(fontSize: 12.5, color: AppColors.textPrimary, height: 1.4)),
          ),
        ],
      ),
    );
  }
}

class _SheetButton extends StatelessWidget {
  const _SheetButton({required this.label, required this.onPressed, this.busy = false});
  final String label;
  final VoidCallback? onPressed;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 54,
      child: FilledButton(
        style: FilledButton.styleFrom(
          backgroundColor: AppColors.primary,
          foregroundColor: Colors.white,
          disabledBackgroundColor: AppColors.surfaceHigh,
          disabledForegroundColor: AppColors.textTertiary,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadii.lg - 2)),
        ),
        onPressed: busy ? null : onPressed,
        child: busy
            ? const SizedBox(
                width: 22,
                height: 22,
                child: CircularProgressIndicator(strokeWidth: 2, valueColor: AlwaysStoppedAnimation(Colors.white)),
              )
            : Text(label, style: GoogleFonts.manrope(fontSize: 15, fontWeight: FontWeight.w800)),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Libellés et formats
// ---------------------------------------------------------------------------

/// Statut affiché de la copie : (pastille, couleur, explication, icône).
(String, Color, String, IconData) _followStatus(FollowOut f) {
  final String? reason = f.pausedReason;
  if (reason != null) {
    return ('EN PAUSE', AppColors.warning, _pausedReasonLabel(reason), Icons.pause_circle_outline_rounded);
  }
  if (!f.active) {
    return (
      'EN PAUSE',
      AppColors.warning,
      'Copie désactivée : réactivez-la depuis « Modifier ».',
      Icons.pause_circle_outline_rounded,
    );
  }
  return (
    'ACTIF',
    AppColors.success,
    'Les positions du trader sont copiées automatiquement sur votre compte.',
    Icons.check_circle_outline_rounded,
  );
}

/// Motif de pause posé par le serveur. Seul « daily_stop_loss » figure au contrat
/// d'API ; les autres codes sont anticipés, un code inconnu est affiché tel quel.
String _pausedReasonLabel(String reason) {
  switch (reason) {
    case 'daily_stop_loss':
      return 'Stop loss journalier atteint, reprise demain';
    case 'user' || 'manual' || 'paused_by_user':
      return 'Copie mise en pause manuellement';
    case 'invalid_token' || 'token_invalid' || 'auth_failed' || 'token_revoked':
      return 'Token Deriv invalide ou révoqué : arrêtez la copie puis recommencez avec un nouveau token';
    case 'insufficient_balance':
      return 'Solde Deriv insuffisant';
    case 'master_disabled' || 'master_inactive' || 'master_removed':
      return 'Ce trader maître a été désactivé';
    case 'subscription_expired' || 'trial_expired' || 'no_subscription':
      return 'Essai ou premium expiré : renouvelez votre abonnement';
    default:
      return _humanize(reason);
  }
}

/// Raison d'un trade ignoré ou en échec (texte serveur affiché tel quel si inconnu).
String _tradeReasonLabel(String reason) {
  switch (reason) {
    case 'daily_stop_loss':
      return 'Stop loss journalier atteint';
    case 'inactive' || 'paused':
      return 'Copie en pause';
    case 'insufficient_balance':
      return 'Solde Deriv insuffisant';
    case 'below_min_stake' || 'min_stake':
      return 'Mise inférieure au minimum Deriv';
    case 'invalid_token' || 'token_invalid' || 'auth_failed':
      return 'Token Deriv invalide ou révoqué';
    default:
      return _humanize(reason);
  }
}

String _humanize(String code) => code.contains(' ') ? code : code.replaceAll('_', ' ');

(String, Color, IconData) _tradeVisual(String status) => switch (status) {
      'open' => ('EN COURS', AppColors.primarySoft, Icons.schedule_rounded),
      'won' => ('GAGNÉ', AppColors.success, Icons.north_east_rounded),
      'lost' => ('PERDU', AppColors.danger, Icons.south_east_rounded),
      'failed' => ('ÉCHEC', AppColors.danger, Icons.error_outline_rounded),
      'skipped' => ('IGNORÉ', AppColors.textTertiary, Icons.block_rounded),
      _ => (status.toUpperCase(), AppColors.textTertiary, Icons.help_outline_rounded),
    };

/// Rise/Fall : CALL / CALLE = hausse, PUT / PUTE = baisse.
String _directionLabel(String contractType) {
  final String t = contractType.toUpperCase();
  if (t.startsWith('CALL')) return 'Hausse';
  if (t.startsWith('PUT')) return 'Baisse';
  return contractType.isEmpty ? 'Contrat' : contractType;
}

String _followersLabel(int n) => '$n abonné${n > 1 ? 's' : ''}';

/// Identifiant de l'utilisateur lu dans le JWT (claim « sub »), null si illisible.
int? _userIdOf(String? jwt) {
  if (jwt == null || jwt.isEmpty) return null;
  final dynamic sub = JwtUtils.decodePayload(jwt)?['sub'];
  return sub == null ? null : int.tryParse(sub.toString());
}

String _money(double v) => v.toStringAsFixed(2);

String _signed(double v) => '${v >= 0 ? '+' : '-'}${v.abs().toStringAsFixed(2)}';

String _withCur(String amount, String currency) => currency.isEmpty ? amount : '$amount $currency';

/// Nombre sans zéros superflus (10 → « 10 », 0.35 → « 0.35 »).
String _fmtPlain(double v) => v.toStringAsFixed(2).replaceFirst(RegExp(r'\.?0+$'), '');

/// Comme [_fmtPlain] mais avec au moins une décimale (1 → « 1.0 »).
String _fmtDecimal(double v) {
  final String p = _fmtPlain(v);
  return p.contains('.') ? p : '$p.0';
}

String _fmtMult(double v) => '×${_fmtDecimal(v)}';

String _fmtWinRate(double? pct) => pct == null ? '—' : '${pct.round()}%';

String _fmtDateTime(DateTime? dt) {
  if (dt == null) return '';
  final l = dt.toLocal();
  String two(int n) => n.toString().padLeft(2, '0');
  return '${two(l.day)}/${two(l.month)} ${two(l.hour)}:${two(l.minute)}';
}

/// Accepte la virgule décimale française.
double? _parseNum(String s) => double.tryParse(s.trim().replaceAll(',', '.'));

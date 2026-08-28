import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../providers/auth_provider.dart';
import '../providers/billing_provider.dart';
import '../providers/bot_provider.dart';
import '../services/bot_service.dart';
import '../theme/app_theme.dart';
import '../widgets/brand_logo.dart';
import 'admin_user_management_screen.dart';
import 'api_token_screen.dart';
import 'login_screen.dart';
import 'premium_screen.dart';
import 'require_admin.dart';

/// Écran principal : header, PnL card, garde-fous, stratégie, trades, CTA.
class DashboardScreen extends ConsumerStatefulWidget {
  const DashboardScreen({super.key});

  @override
  ConsumerState<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends ConsumerState<DashboardScreen> {
  static const List<String> _strategies = ['RISE_FALL', 'OVER_UNDER', 'MARTINGALE'];
  static const Set<String> _activeStates = {'RUNNING', 'PAUSED'};

  String _symbol = 'R_100';
  double _stake = 1.0;
  double _stopLoss = 10.0;
  double _takeProfit = 20.0;
  String _strategy = 'RISE_FALL';
  String _accountType = 'demo'; // demo | real
  bool _busy = false;

  BotService get _service => ref.read(botServiceProvider);

  Future<void> _start() async {
    final token = ref.read(tokenProvider) ??
        await ref.read(secureStorageProvider).read(key: kTokenKey);
    if (token == null || token.isEmpty) {
      _snack('Token manquant');
      return;
    }
    setState(() => _busy = true);
    try {
      final jwt = ref.read(jwtProvider);
      await _service.startBot(
        token: token,
        symbol: _symbol,
        stake: _stake,
        stopLoss: _stopLoss,
        takeProfit: _takeProfit,
        strategy: _strategy,
        accountType: _accountType,
        jwt: jwt,
      );
      _snack('Robot démarré sur $_symbol');
    } on BotServiceException catch (e) {
      if (e.statusCode == 402) {
        _openPremium(reason: e.detail);
      } else {
        _snack(e.toString());
      }
    } catch (e) {
      _snack('Échec : $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _openPremium({String? reason}) {
    if (reason != null && reason.isNotEmpty) _snack(reason);
    Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => const PremiumScreen()));
  }

  Future<void> _stop() async {
    setState(() => _busy = true);
    try {
      await _service.stopBot();
      _snack('Robot mis en pause');
    } on BotServiceException catch (e) {
      _snack(e.toString());
    } catch (e) {
      _snack('Échec : $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _snack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _logout() async {
    await ref.read(tokenProvider.notifier).clear();
    if (!mounted) return;
    Navigator.of(context).pushReplacement(
      MaterialPageRoute<void>(builder: (_) => const ApiTokenScreen()),
    );
  }

  void _openLogin() {
    Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => const LoginScreen()),
    );
  }

  void _openAdmin() {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => const RequireAdmin(child: AdminUserManagementScreen()),
      ),
    );
  }

  void _openConfigSheet() {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: AppColors.bg,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
      ),
      builder: (_) => _ConfigSheet(
        symbol: _symbol,
        stake: _stake,
        stopLoss: _stopLoss,
        takeProfit: _takeProfit,
        strategy: _strategy,
        strategies: _strategies,
        onApply: (s, stake, sl, tp, strat) {
          setState(() {
            _symbol = s;
            _stake = stake;
            _stopLoss = sl;
            _takeProfit = tp;
            _strategy = strat;
          });
          Navigator.of(context).pop();
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final statusAsync = ref.watch(botStatusStreamProvider);
    final isAdmin = ref.watch(isAdminProvider);
    final isLoggedIn = ref.watch(jwtProvider) != null;
    final Map<String, dynamic> status = statusAsync.maybeWhen(
      data: (d) => d,
      orElse: () => const <String, dynamic>{},
    );

    final String state = (status['state'] as String?) ?? 'STOPPED';
    final bool isActive = _activeStates.contains(state);
    final double pnl = (status['pnl'] as num?)?.toDouble() ?? 0.0;
    final double balance = (status['current_balance'] as num?)?.toDouble() ?? 0.0;
    final String currency = (status['currency'] as String?) ?? 'USD';
    final int won = (status['trades_won'] as num?)?.toInt() ?? 0;
    final int lost = (status['trades_lost'] as num?)?.toInt() ?? 0;
    final int total = won + lost;
    final String winRate = total == 0 ? '—' : '${((won / total) * 100).round()}%';

    final Color pnlColor = pnl >= 0 ? AppColors.success : AppColors.danger;
    final (String pillText, Color pillColor) = _stateVisual(state);

    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            _Header(
              isRunning: isActive,
              pillText: pillText,
              pillColor: pillColor,
              isAdmin: isAdmin,
              isLoggedIn: isLoggedIn,
              onLogout: _logout,
              onLogin: _openLogin,
              onAdmin: _openAdmin,
            ),
            Expanded(
              child: RefreshIndicator(
                onRefresh: () async => ref.invalidate(botStatusStreamProvider),
                color: AppColors.primary,
                backgroundColor: AppColors.surface,
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(20, 4, 20, 20),
                  children: [
                    _SubscriptionBanner(
                      isLoggedIn: isLoggedIn,
                      onLogin: _openLogin,
                      onOpenPremium: () => _openPremium(),
                    ),
                    const SizedBox(height: 14),
                    _AccountTypeSwitch(
                      value: _accountType,
                      onChanged: (v) => setState(() => _accountType = v),
                    ),
                    const SizedBox(height: 14),
                    _PnlCard(
                      pnl: pnl,
                      balance: balance,
                      currency: currency,
                      wins: won,
                      losses: lost,
                      winRate: winRate,
                      pnlColor: pnlColor,
                    ),
                    const SizedBox(height: 14),
                    _GuardsCard(
                      pnl: pnl,
                      stopLoss: _stopLoss,
                      takeProfit: _takeProfit,
                      currency: currency,
                      onEdit: _openConfigSheet,
                    ),
                    const SizedBox(height: 14),
                    _StrategyCard(
                      strategy: _strategy,
                      symbol: _symbol,
                      stake: _stake,
                      currency: currency,
                      onTap: _openConfigSheet,
                    ),
                    const SizedBox(height: 20),
                    Row(
                      children: [
                        Text('Derniers trades', style: AppTheme.heading(fontSize: 13, letterSpacing: 0.2)),
                      ],
                    ),
                    const SizedBox(height: 12),
                    _TradesList(trades: status['last_trades']),
                    const SizedBox(height: 20),
                  ],
                ),
              ),
            ),
            _BottomCta(isActive: isActive, busy: _busy, onStart: _start, onStop: _stop),
          ],
        ),
      ),
    );
  }

  (String, Color) _stateVisual(String state) {
    switch (state) {
      case 'RUNNING':
        return ('EN MARCHE', AppColors.success);
      case 'PAUSED':
        return ('EN PAUSE', AppColors.warning);
      case 'STOP_LOSS_REACHED':
        return ('STOP LOSS', AppColors.danger);
      case 'TAKE_PROFIT_REACHED':
        return ('TAKE PROFIT', AppColors.primarySoft);
      case 'ERROR':
        return ('ERREUR', AppColors.danger);
      default:
        return ('EN PAUSE', AppColors.warning);
    }
  }
}

class _Header extends StatelessWidget {
  const _Header({
    required this.isRunning,
    required this.pillText,
    required this.pillColor,
    required this.isAdmin,
    required this.isLoggedIn,
    required this.onLogout,
    required this.onLogin,
    required this.onAdmin,
  });

  final bool isRunning;
  final String pillText;
  final Color pillColor;
  final bool isAdmin;
  final bool isLoggedIn;
  final VoidCallback onLogout;
  final VoidCallback onLogin;
  final VoidCallback onAdmin;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 14),
      child: Row(
        children: [
          const BrandLogo(size: 38, letter: 'D'),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Deriv Trading Bot',
                    style: AppTheme.heading(fontSize: 14.5, letterSpacing: -0.2)),
                const SizedBox(height: 2),
                Text('Compte démo · Deriv',
                    style: GoogleFonts.manrope(
                        fontSize: 11, color: AppColors.textTertiary, fontWeight: FontWeight.w600, letterSpacing: 0.3)),
              ],
            ),
          ),
          StatusPill(label: pillText, color: pillColor),
          const SizedBox(width: 8),
          if (isAdmin)
            _IconChip(icon: Icons.admin_panel_settings_outlined, tooltip: 'Admin', onPressed: onAdmin)
          else if (!isLoggedIn)
            _IconChip(icon: Icons.person_outline, tooltip: 'Connexion', onPressed: onLogin),
          const SizedBox(width: 6),
          _IconChip(icon: Icons.logout_rounded, tooltip: 'Changer de token', onPressed: onLogout),
        ],
      ),
    );
  }
}

class _IconChip extends StatelessWidget {
  const _IconChip({required this.icon, required this.onPressed, this.tooltip});
  final IconData icon;
  final VoidCallback onPressed;
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final btn = Container(
      width: 34,
      height: 34,
      decoration: BoxDecoration(
        color: AppColors.surfaceAlt,
        borderRadius: BorderRadius.circular(11),
        border: Border.all(color: AppColors.border, width: 1),
      ),
      child: IconButton(
        padding: EdgeInsets.zero,
        onPressed: onPressed,
        icon: Icon(icon, size: 16, color: AppColors.textTertiary),
      ),
    );
    return tooltip == null ? btn : Tooltip(message: tooltip!, child: btn);
  }
}

class _PnlCard extends StatelessWidget {
  const _PnlCard({
    required this.pnl,
    required this.balance,
    required this.currency,
    required this.wins,
    required this.losses,
    required this.winRate,
    required this.pnlColor,
  });

  final double pnl;
  final double balance;
  final String currency;
  final int wins;
  final int losses;
  final String winRate;
  final Color pnlColor;

  @override
  Widget build(BuildContext context) {
    final sign = pnl >= 0 ? '+' : '-';
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: AppTheme.cardGradient(),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('PNL DE LA SESSION', style: AppTheme.labelMicro(color: AppColors.textTertiary).copyWith(fontSize: 11, letterSpacing: 0.9)),
                    const SizedBox(height: 9),
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.baseline,
                      textBaseline: TextBaseline.alphabetic,
                      children: [
                        Text('$sign${pnl.abs().toStringAsFixed(2)}',
                            style: AppTheme.mono(fontSize: 35, fontWeight: FontWeight.w700, letterSpacing: -1.5, color: pnlColor)),
                        const SizedBox(width: 8),
                        Text(currency,
                            style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
                      ],
                    ),
                  ],
                ),
              ),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Text('SOLDE', style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.9)),
                  const SizedBox(height: 9),
                  Text(balance.toStringAsFixed(2), style: AppTheme.mono(fontSize: 16, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                ],
              ),
            ],
          ),
          const SizedBox(height: 14),
          Row(
            children: [
              Expanded(child: _MiniStat(label: 'GAGNÉS', value: '$wins', color: AppColors.success)),
              const SizedBox(width: 10),
              Expanded(child: _MiniStat(label: 'PERDUS', value: '$losses', color: AppColors.danger)),
              const SizedBox(width: 10),
              Expanded(child: _MiniStat(label: 'RÉUSSITE', value: winRate, color: AppColors.textPrimary)),
            ],
          ),
        ],
      ),
    );
  }
}

class _MiniStat extends StatelessWidget {
  const _MiniStat({required this.label, required this.value, required this.color});
  final String label;
  final String value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.035),
        borderRadius: BorderRadius.circular(13),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: AppTheme.labelMicro()),
          const SizedBox(height: 5),
          Text(value, style: AppTheme.mono(fontSize: 16, fontWeight: FontWeight.w700, color: color)),
        ],
      ),
    );
  }
}

class _GuardsCard extends StatelessWidget {
  const _GuardsCard({
    required this.pnl,
    required this.stopLoss,
    required this.takeProfit,
    required this.currency,
    required this.onEdit,
  });

  final double pnl;
  final double stopLoss;
  final double takeProfit;
  final String currency;
  final VoidCallback onEdit;

  @override
  Widget build(BuildContext context) {
    final double slUsed = pnl < 0 ? pnl.abs() : 0;
    final double tpUsed = pnl > 0 ? pnl : 0;
    final double slPct = stopLoss <= 0 ? 0 : (slUsed / stopLoss).clamp(0, 1);
    final double tpPct = takeProfit <= 0 ? 0 : (tpUsed / takeProfit).clamp(0, 1);

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
      decoration: AppTheme.card(radius: AppRadii.lg + 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text('Garde-fous du jour', style: AppTheme.heading(fontSize: 12.5, letterSpacing: 0.2)),
              const Spacer(),
              InkWell(
                onTap: onEdit,
                child: Text('Modifier', style: GoogleFonts.manrope(fontSize: 12, color: AppColors.primarySoft, fontWeight: FontWeight.w700)),
              ),
            ],
          ),
          const SizedBox(height: 14),
          _GuardRow(label: 'Stop loss', used: slUsed, cap: stopLoss, currency: currency, pct: slPct, color: AppColors.danger),
          const SizedBox(height: 13),
          _GuardRow(label: 'Take profit', used: tpUsed, cap: takeProfit, currency: currency, pct: tpPct, color: AppColors.success),
        ],
      ),
    );
  }
}

class _GuardRow extends StatelessWidget {
  const _GuardRow({
    required this.label,
    required this.used,
    required this.cap,
    required this.currency,
    required this.pct,
    required this.color,
  });

  final String label;
  final double used;
  final double cap;
  final String currency;
  final double pct;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label, style: GoogleFonts.manrope(fontSize: 11.5, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
            Text('${used.toStringAsFixed(2)} / ${cap.toStringAsFixed(0)} $currency',
                style: AppTheme.mono(fontSize: 11.5, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
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
              decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(999)),
            ),
          ),
        ),
      ],
    );
  }
}

class _StrategyCard extends StatelessWidget {
  const _StrategyCard({
    required this.strategy,
    required this.symbol,
    required this.stake,
    required this.currency,
    required this.onTap,
  });

  final String strategy;
  final String symbol;
  final double stake;
  final String currency;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final label = switch (strategy) {
      'RISE_FALL' => 'Rise / Fall',
      'OVER_UNDER' => 'Over / Under',
      'MARTINGALE' => 'Martingale',
      _ => strategy,
    };
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(AppRadii.lg + 2),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 15),
        decoration: AppTheme.card(radius: AppRadii.lg + 2),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('STRATÉGIE ACTIVE', style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.8)),
                  const SizedBox(height: 4),
                  Text(label, style: AppTheme.heading(fontSize: 14.5, letterSpacing: -0.2)),
                ],
              ),
            ),
            _MonoChip(text: symbol, color: AppColors.primarySoft, bg: AppColors.primary.withValues(alpha: 0.14)),
            const SizedBox(width: 6),
            _MonoChip(text: '${stake.toStringAsFixed(2)} $currency', color: AppColors.textSecondary, bg: Colors.white.withValues(alpha: 0.05)),
            const SizedBox(width: 4),
            const Icon(Icons.chevron_right_rounded, color: AppColors.textTertiary, size: 20),
          ],
        ),
      ),
    );
  }
}

class _MonoChip extends StatelessWidget {
  const _MonoChip({required this.text, required this.color, required this.bg});
  final String text;
  final Color color;
  final Color bg;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(9)),
      child: Text(text, style: AppTheme.mono(fontSize: 11, fontWeight: FontWeight.w700, color: color)),
    );
  }
}

class _TradesList extends StatelessWidget {
  const _TradesList({required this.trades});
  final dynamic trades;

  @override
  Widget build(BuildContext context) {
    final List<Map<String, dynamic>> items = trades is List
        ? (trades as List).whereType<Map<String, dynamic>>().toList(growable: false)
        : const <Map<String, dynamic>>[];

    if (items.isEmpty) {
      return Container(
        padding: const EdgeInsets.all(24),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(AppRadii.lg),
          border: Border.all(color: Colors.white.withValues(alpha: 0.12), width: 1, style: BorderStyle.solid),
        ),
        child: Column(
          children: [
            Text('Aucun trade sur cette session',
                style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textSecondary)),
            const SizedBox(height: 6),
            Text('Démarrez le robot pour ouvrir la première position.',
                style: GoogleFonts.manrope(fontSize: 12, color: AppColors.textTertiary)),
          ],
        ),
      );
    }

    return Column(
      children: [
        for (final t in items) ...[
          _TradeRow(trade: t),
          const SizedBox(height: 8),
        ],
      ],
    );
  }
}

class _TradeRow extends StatelessWidget {
  const _TradeRow({required this.trade});
  final Map<String, dynamic> trade;

  @override
  Widget build(BuildContext context) {
    final double profit = (trade['profit'] as num?)?.toDouble() ?? 0.0;
    final bool won = (trade['result'] as String?) == 'won';
    final Color color = won ? AppColors.success : AppColors.danger;
    final Color chipBg = color.withValues(alpha: 0.13);
    final String type = (trade['contract_type'] as String?) ?? '';
    final String dirLabel = type.contains('CALL') ? 'Hausse' : (type.contains('PUT') ? 'Baisse' : type);
    final String arrow = won ? '▲' : '▼';
    final String symbol = (trade['symbol'] as String?) ?? '';
    final double stake = (trade['stake'] as num?)?.toDouble() ?? 0.0;
    final String time = _fmtTime(trade['timestamp']);
    final String sign = profit >= 0 ? '+' : '-';

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: AppTheme.card(radius: AppRadii.md + 2, border: AppColors.borderSoft),
      child: Row(
        children: [
          Container(
            width: 34,
            height: 34,
            decoration: BoxDecoration(color: chipBg, borderRadius: BorderRadius.circular(11)),
            alignment: Alignment.center,
            child: Text(arrow, style: GoogleFonts.manrope(color: color, fontSize: 13, fontWeight: FontWeight.w800)),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('$dirLabel · $symbol',
                    style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                const SizedBox(height: 3),
                Text('$time · mise ${stake.toStringAsFixed(2)}',
                    style: AppTheme.mono(fontSize: 10.5, color: AppColors.textTertiary)),
              ],
            ),
          ),
          Text('$sign${profit.abs().toStringAsFixed(2)} \$',
              style: AppTheme.mono(fontSize: 14, fontWeight: FontWeight.w700, color: color)),
        ],
      ),
    );
  }

  String _fmtTime(dynamic epoch) {
    final double? seconds = (epoch as num?)?.toDouble();
    if (seconds == null) return '';
    final dt = DateTime.fromMillisecondsSinceEpoch((seconds * 1000).round()).toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(dt.hour)}:${two(dt.minute)}:${two(dt.second)}';
  }
}

class _BottomCta extends StatelessWidget {
  const _BottomCta({required this.isActive, required this.busy, required this.onStart, required this.onStop});

  final bool isActive;
  final bool busy;
  final VoidCallback onStart;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    final label = isActive ? 'Arrêter le robot' : 'Démarrer le robot';
    final icon = isActive ? Icons.stop_rounded : Icons.play_arrow_rounded;

    return Container(
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [AppColors.bg.withValues(alpha: 0.0), AppColors.bg],
          stops: const [0.0, 0.55],
        ),
      ),
      child: SizedBox(
        height: 58,
        child: Material(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(AppRadii.lg),
          child: Ink(
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(AppRadii.lg),
              gradient: isActive
                  ? null
                  : const LinearGradient(
                      begin: Alignment(-0.5, -1),
                      end: Alignment(1, 1),
                      colors: [AppColors.success, Color(0xFF22A877)],
                    ),
              color: isActive ? AppColors.danger.withValues(alpha: 0.14) : null,
              boxShadow: isActive
                  ? null
                  : [
                      BoxShadow(color: AppColors.success.withValues(alpha: 0.55), blurRadius: 30, offset: const Offset(0, 14), spreadRadius: -14),
                    ],
            ),
            child: InkWell(
              borderRadius: BorderRadius.circular(AppRadii.lg),
              onTap: busy ? null : (isActive ? onStop : onStart),
              child: Center(
                child: busy
                    ? const SizedBox(
                        width: 22,
                        height: 22,
                        child: CircularProgressIndicator(strokeWidth: 2, valueColor: AlwaysStoppedAnimation(Colors.white)),
                      )
                    : Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(icon, size: 20, color: isActive ? AppColors.danger : const Color(0xFF04231A)),
                          const SizedBox(width: 8),
                          Text(label,
                              style: GoogleFonts.manrope(
                                fontSize: 16,
                                fontWeight: FontWeight.w800,
                                letterSpacing: 0.3,
                                color: isActive ? AppColors.danger : const Color(0xFF04231A),
                              )),
                        ],
                      ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Bottom-sheet de config : symbole / stratégie / mise / SL / TP.
class _ConfigSheet extends StatefulWidget {
  const _ConfigSheet({
    required this.symbol,
    required this.stake,
    required this.stopLoss,
    required this.takeProfit,
    required this.strategy,
    required this.strategies,
    required this.onApply,
  });

  final String symbol;
  final double stake;
  final double stopLoss;
  final double takeProfit;
  final String strategy;
  final List<String> strategies;
  final void Function(String symbol, double stake, double sl, double tp, String strategy) onApply;

  @override
  State<_ConfigSheet> createState() => _ConfigSheetState();
}

class _ConfigSheetState extends State<_ConfigSheet> {
  static const List<String> _symbols = [
    'R_10', 'R_25', 'R_50', 'R_75', 'R_100', 'BOOM500', 'CRASH500'
  ];

  late String _symbol = widget.symbol;
  late String _strategy = widget.strategy;
  late double _stake = widget.stake;
  late double _sl = widget.stopLoss;
  late double _tp = widget.takeProfit;

  void _bump(String kind, double delta) {
    setState(() {
      switch (kind) {
        case 'stake':
          _stake = (_stake + delta).clamp(0.5, 1000);
        case 'sl':
          _sl = (_sl + delta).clamp(1, 10000);
        case 'tp':
          _tp = (_tp + delta).clamp(1, 10000);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final media = MediaQuery.of(context);
    return Padding(
      padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
      child: SafeArea(
        top: false,
        child: Container(
          padding: const EdgeInsets.fromLTRB(22, 10, 22, 20),
          decoration: const BoxDecoration(
            color: AppColors.bg,
            borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
          ),
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
              Text('Paramètres du robot', style: AppTheme.heading(fontSize: 21, letterSpacing: -0.5)),
              const SizedBox(height: 14),
              _sectionCard(
                title: 'INDICE',
                child: Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final s in _symbols) _pill(s, selected: _symbol == s, onTap: () => setState(() => _symbol = s)),
                  ],
                ),
              ),
              const SizedBox(height: 14),
              _sectionCard(
                title: 'STRATÉGIE',
                child: Column(
                  children: [
                    for (final st in widget.strategies)
                      _strategyRow(st, selected: _strategy == st, onTap: () => setState(() => _strategy = st)),
                  ],
                ),
              ),
              const SizedBox(height: 14),
              _sectionCard(
                title: 'GESTION DU RISQUE',
                child: Column(
                  children: [
                    _numericRow('Mise par trade', _stake.toStringAsFixed(2), () => _bump('stake', -0.5), () => _bump('stake', 0.5)),
                    const SizedBox(height: 14),
                    _numericRow('Stop loss du jour', _sl.toStringAsFixed(0), () => _bump('sl', -1), () => _bump('sl', 1)),
                    const SizedBox(height: 14),
                    _numericRow('Take profit du jour', _tp.toStringAsFixed(0), () => _bump('tp', -1), () => _bump('tp', 1)),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              SizedBox(
                height: 54,
                child: FilledButton(
                  style: FilledButton.styleFrom(
                    backgroundColor: AppColors.primary.withValues(alpha: 0.16),
                    foregroundColor: AppColors.primarySoft,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadii.lg - 2)),
                  ),
                  onPressed: () => widget.onApply(_symbol, _stake, _sl, _tp, _strategy),
                  child: Text('Appliquer et revenir au robot',
                      style: GoogleFonts.manrope(fontSize: 15, fontWeight: FontWeight.w800)),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _sectionCard({required String title, required Widget child}) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
        decoration: AppTheme.card(radius: AppRadii.lg + 2),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.8)),
            const SizedBox(height: 12),
            child,
          ],
        ),
      );

  Widget _pill(String name, {required bool selected, required VoidCallback onTap}) {
    return InkWell(
      borderRadius: BorderRadius.circular(11),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 9),
        decoration: BoxDecoration(
          color: selected ? AppColors.primary.withValues(alpha: 0.18) : Colors.white.withValues(alpha: 0.04),
          border: Border.all(
            color: selected ? AppColors.primary.withValues(alpha: 0.5) : AppColors.border,
            width: 1,
          ),
          borderRadius: BorderRadius.circular(11),
        ),
        child: Text(name,
            style: AppTheme.mono(
              fontSize: 12,
              fontWeight: FontWeight.w700,
              color: selected ? AppColors.primarySoft : AppColors.textSecondary,
            )),
      ),
    );
  }

  Widget _strategyRow(String value, {required bool selected, required VoidCallback onTap}) {
    final label = switch (value) {
      'RISE_FALL' => 'Rise / Fall',
      'OVER_UNDER' => 'Over / Under',
      'MARTINGALE' => 'Martingale',
      _ => value,
    };
    final desc = switch (value) {
      'RISE_FALL' => 'Direction du prochain tick',
      'OVER_UNDER' => 'Seuil sur le dernier chiffre',
      'MARTINGALE' => 'Doublement de mise après une perte',
      _ => '',
    };
    final risk = switch (value) {
      'RISE_FALL' => 'Risque modéré',
      'OVER_UNDER' => 'Risque élevé',
      'MARTINGALE' => 'Risque très élevé',
      _ => '',
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(14),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 13),
          decoration: BoxDecoration(
            color: selected ? AppColors.primary.withValues(alpha: 0.12) : Colors.white.withValues(alpha: 0.025),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(
              color: selected ? AppColors.primary.withValues(alpha: 0.45) : AppColors.borderSoft,
              width: 1,
            ),
          ),
          child: Row(
            children: [
              Container(
                width: 18,
                height: 18,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: selected ? AppColors.primary : Colors.white.withValues(alpha: 0.2),
                    width: 2,
                  ),
                ),
                alignment: Alignment.center,
                child: Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: selected ? AppColors.primary : Colors.transparent,
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(label, style: GoogleFonts.manrope(fontSize: 13.5, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                    const SizedBox(height: 3),
                    Text(desc, style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary)),
                  ],
                ),
              ),
              Text(risk, style: AppTheme.mono(fontSize: 10.5, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _numericRow(String label, String value, VoidCallback dec, VoidCallback inc) {
    return Row(
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(label, style: GoogleFonts.manrope(fontSize: 13.5, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
              const SizedBox(height: 3),
              Text('Ajuster à l\'unité', style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary)),
            ],
          ),
        ),
        Container(
          padding: const EdgeInsets.all(4),
          decoration: BoxDecoration(
            color: AppColors.surfaceHigh,
            borderRadius: BorderRadius.circular(13),
            border: Border.all(color: AppColors.border, width: 1),
          ),
          child: Row(
            children: [
              _stepper('−', dec),
              SizedBox(
                width: 56,
                child: Center(
                  child: Text(value, style: AppTheme.mono(fontSize: 14, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                ),
              ),
              _stepper('+', inc),
            ],
          ),
        ),
      ],
    );
  }

  Widget _stepper(String label, VoidCallback onTap) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(9),
      child: SizedBox(
        width: 32,
        height: 32,
        child: Center(
          child: Text(label, style: GoogleFonts.manrope(fontSize: 17, fontWeight: FontWeight.w700, color: AppColors.textSecondary)),
        ),
      ),
    );
  }
}

/// Bandeau affichant l'état de l'essai / du premium avec CTA vers l'écran Premium.
class _SubscriptionBanner extends ConsumerWidget {
  const _SubscriptionBanner({
    required this.isLoggedIn,
    required this.onLogin,
    required this.onOpenPremium,
  });

  final bool isLoggedIn;
  final VoidCallback onLogin;
  final VoidCallback onOpenPremium;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final statusAsync = ref.watch(subscriptionStatusProvider);
    if (!isLoggedIn) {
      return _pill(
        icon: Icons.person_outline,
        color: AppColors.textTertiary,
        title: 'Non connecté',
        subtitle: 'Compte requis pour l\'essai gratuit et le premium',
        cta: 'Se connecter',
        onTap: onLogin,
      );
    }
    return statusAsync.when(
      data: (s) {
        if (s == null) return const SizedBox.shrink();
        if (s.premiumActive) {
          return _pill(
            icon: Icons.workspace_premium_rounded,
            color: AppColors.success,
            title: 'Premium actif',
            subtitle: s.premiumExpiresAt == null
                ? 'Accès illimité'
                : 'Jusqu\'au ${_fmtDate(s.premiumExpiresAt!)}',
          );
        }
        if (s.trialActive) {
          return _pill(
            icon: Icons.timer_outlined,
            color: AppColors.primarySoft,
            title: 'Essai gratuit — ${s.trialDaysRemaining} jour${s.trialDaysRemaining > 1 ? "s" : ""} restant',
            subtitle: 'Après cette période, passez au premium pour le compte réel.',
            cta: 'Voir les formules',
            onTap: onOpenPremium,
          );
        }
        return _pill(
          icon: Icons.lock_outline_rounded,
          color: AppColors.danger,
          title: 'Essai terminé',
          subtitle: 'Passez au premium pour trader en compte réel.',
          cta: 'Passer au premium',
          onTap: onOpenPremium,
        );
      },
      loading: () => const SizedBox.shrink(),
      error: (_, __) => const SizedBox.shrink(),
    );
  }

  Widget _pill({
    required IconData icon,
    required Color color,
    required String title,
    required String subtitle,
    String? cta,
    VoidCallback? onTap,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md + 2),
        border: Border.all(color: color.withValues(alpha: 0.35), width: 1),
      ),
      child: Row(
        children: [
          Icon(icon, size: 20, color: color),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(title, style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w800, color: AppColors.textPrimary)),
                const SizedBox(height: 2),
                Text(subtitle, style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.35)),
              ],
            ),
          ),
          if (cta != null && onTap != null)
            TextButton(
              onPressed: onTap,
              style: TextButton.styleFrom(padding: const EdgeInsets.symmetric(horizontal: 10)),
              child: Text(cta,
                  style: GoogleFonts.manrope(fontSize: 12, fontWeight: FontWeight.w800, color: color)),
            ),
        ],
      ),
    );
  }

  String _fmtDate(DateTime dt) {
    final l = dt.toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(l.day)}/${two(l.month)}/${l.year}';
  }
}

/// Switch segmenté demo / real.
class _AccountTypeSwitch extends StatelessWidget {
  const _AccountTypeSwitch({required this.value, required this.onChanged});
  final String value;
  final ValueChanged<String> onChanged;

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
        onTap: () => onChanged(key),
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

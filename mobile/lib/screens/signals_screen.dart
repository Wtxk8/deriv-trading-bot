import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../providers/auth_provider.dart';
import '../providers/signal_provider.dart';
import '../services/signal_service.dart';
import '../theme/app_theme.dart';
import '../widgets/brand_logo.dart';
import 'login_screen.dart';
import 'premium_screen.dart';

/// Avertissement permanent affiché en tête de l'écran Signaux.
const String kSignalsDisclaimer =
    'Signaux générés automatiquement, à titre indicatif. Les indices synthétiques Deriv '
    'sont produits par un générateur aléatoire : aucun signal ne garantit un gain. '
    'Ne risquez que ce que vous pouvez perdre.';

enum _StatusFilter { all, active, closed }

/// Écran Signaux : avertissement, performance réelle, filtres et liste temps réel.
class SignalsScreen extends ConsumerStatefulWidget {
  const SignalsScreen({super.key});

  @override
  ConsumerState<SignalsScreen> createState() => _SignalsScreenState();
}

class _SignalsScreenState extends ConsumerState<SignalsScreen> {
  _StatusFilter _statusFilter = _StatusFilter.all;
  String? _symbolFilter; // null = tous les indices

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && ref.read(signalsProvider).liveAccess) _askNotificationPermission();
    });
  }

  /// Android 13+ : la demande n'a de sens que si des signaux en direct peuvent arriver.
  void _askNotificationPermission() {
    unawaited(ref.read(notificationServiceProvider).requestPermission());
  }

  Future<void> _refresh() => ref.read(signalsProvider.notifier).refresh();

  Future<void> _openPremium() async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => const PremiumScreen()),
    );
    if (!mounted) return;
    // Un abonnement a pu être activé : droits et liste rechargés (flux rouvert si besoin).
    await _refresh();
  }

  void _openLogin() {
    // LoginScreen remplace sa route par le tableau de bord une fois connecté.
    Navigator.of(context).pushReplacement(
      MaterialPageRoute<void>(builder: (_) => const LoginScreen()),
    );
  }

  bool _matches(Signal signal) {
    final statusOk = switch (_statusFilter) {
      _StatusFilter.all => true,
      _StatusFilter.active => signal.isActive,
      _StatusFilter.closed => signal.isClosed,
    };
    return statusOk && (_symbolFilter == null || signal.symbol == _symbolFilter);
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<bool>(signalsProvider.select((s) => s.liveAccess), (previous, next) {
      if (next && previous != true) _askNotificationPermission();
    });

    final state = ref.watch(signalsProvider);
    final isLoggedIn = (ref.watch(jwtProvider) ?? '').isNotEmpty;
    final symbolFilter = _symbolFilter;
    final symbols = <String>{
      for (final s in state.signals)
        if (s.symbol.isNotEmpty) s.symbol,
      if (symbolFilter != null) symbolFilter,
    }.toList()
      ..sort();
    final visible = state.signals.where(_matches).toList(growable: false);
    // Droits pas encore confirmés (chargement en cours ou en échec) : ni pastille ni verrou à tort.
    final accessKnown = isLoggedIn && state.accessKnown;

    return Scaffold(
      appBar: AppBar(
        title: Text('Signaux', style: AppTheme.heading(fontSize: 15, letterSpacing: -0.2)),
        actions: [
          if (accessKnown)
            Padding(
              padding: const EdgeInsets.only(right: 16),
              child: Center(
                child: state.liveAccess
                    ? const StatusPill(label: 'EN DIRECT', color: AppColors.success)
                    : const StatusPill(label: 'HISTORIQUE', color: AppColors.warning),
              ),
            ),
        ],
      ),
      body: SafeArea(
        top: false,
        child: Column(
          children: [
            // Avertissement permanent : épinglé au-dessus de la liste, jamais masqué par le défilement.
            const Padding(
              padding: EdgeInsets.fromLTRB(20, 4, 20, 10),
              child: _DisclaimerBanner(),
            ),
            Expanded(
              child: RefreshIndicator(
                onRefresh: _refresh,
                color: AppColors.primary,
                backgroundColor: AppColors.surface,
                child: ListView(
                  physics: const AlwaysScrollableScrollPhysics(),
                  padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
                  children: [
                    _PerformanceCard(report: state.stats),
                    if (accessKnown && !state.liveAccess) ...[
                      const SizedBox(height: 14),
                      _LockedCard(onOpenPremium: _openPremium),
                    ],
                    const SizedBox(height: 18),
                    _StatusSegments(
                      value: _statusFilter,
                      onChanged: (v) => setState(() => _statusFilter = v),
                    ),
                    if (symbols.isNotEmpty) ...[
                      const SizedBox(height: 10),
                      _SymbolChips(
                        symbols: symbols,
                        selected: symbolFilter,
                        onChanged: (v) => setState(() => _symbolFilter = v),
                      ),
                    ],
                    const SizedBox(height: 20),
                    Row(
                      children: [
                        Text('Signaux récents', style: AppTheme.heading(fontSize: 13, letterSpacing: 0.2)),
                        const Spacer(),
                        if (visible.isNotEmpty)
                          Text('${visible.length}',
                              style: AppTheme.mono(
                                  fontSize: 11.5, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
                      ],
                    ),
                    const SizedBox(height: 12),
                    ..._content(state, visible, isLoggedIn),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  List<Widget> _content(SignalsState state, List<Signal> visible, bool isLoggedIn) {
    if (!isLoggedIn) {
      return [
        _MessageCard(
          icon: Icons.person_outline,
          color: AppColors.primarySoft,
          title: 'Connexion requise',
          subtitle: 'Connectez-vous pour consulter les signaux et leur performance.',
          cta: 'Se connecter',
          onTap: _openLogin,
        ),
      ];
    }
    if (state.loading && state.signals.isEmpty) {
      return const [
        Padding(
          padding: EdgeInsets.symmetric(vertical: 36),
          child: Center(child: CircularProgressIndicator(color: AppColors.primary)),
        ),
      ];
    }
    final error = state.error;
    if (error != null && state.signals.isEmpty) {
      return [
        _MessageCard(
          icon: Icons.cloud_off_rounded,
          color: AppColors.danger,
          title: 'Impossible de charger les signaux',
          subtitle: error,
          cta: 'Réessayer',
          onTap: () => unawaited(_refresh()),
        ),
      ];
    }
    final (emptyTitle, emptySubtitle) = _emptyTexts(state);
    return [
      if (error != null) ...[
        _InlineError(message: error),
        const SizedBox(height: 12),
      ],
      if (visible.isEmpty)
        _EmptyState(title: emptyTitle, subtitle: emptySubtitle)
      else
        for (final s in visible) ...[
          _SignalCard(key: ValueKey<int>(s.id), signal: s),
          const SizedBox(height: 10),
        ],
    ];
  }

  (String, String) _emptyTexts(SignalsState state) {
    if (state.signals.isEmpty) {
      return state.liveAccess
          ? ('Aucun signal pour le moment', 'Les nouveaux signaux apparaîtront ici en temps réel.')
          : ('Aucun signal clôturé pour le moment', 'L\'historique des signaux clôturés apparaîtra ici.');
    }
    if (_statusFilter == _StatusFilter.active && !state.liveAccess) {
      return ('Signaux actifs verrouillés', 'Les signaux en cours sont réservés à l\'essai gratuit et au Premium.');
    }
    return ('Aucun signal ne correspond à ces filtres', 'Modifiez le statut ou l\'indice sélectionné.');
  }
}

class _DisclaimerBanner extends StatelessWidget {
  const _DisclaimerBanner();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: AppColors.warning.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md + 2),
        border: Border.all(color: AppColors.warning.withValues(alpha: 0.35), width: 1),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Padding(
            padding: EdgeInsets.only(top: 1),
            child: Icon(Icons.warning_amber_rounded, size: 20, color: AppColors.warning),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              kSignalsDisclaimer,
              style: GoogleFonts.manrope(
                fontSize: 11.5,
                fontWeight: FontWeight.w600,
                color: AppColors.textSecondary,
                height: 1.4,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Performance réelle calculée par le serveur sur les signaux clôturés.
class _PerformanceCard extends StatelessWidget {
  const _PerformanceCard({required this.report});
  final SignalStatsReport? report;

  @override
  Widget build(BuildContext context) {
    final overall = report?.overall ?? SignalStats.empty;
    // Statistiques indisponibles : « — » plutôt que des zéros trompeurs.
    String count(int n) => report == null ? '—' : '$n';
    final days = report?.windowDays ?? kSignalStatsDays;
    final strategies = <String>{...kSignalStrategies, ...?report?.byStrategy.keys};

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
                    Text('PERFORMANCE RÉELLE · $days JOURS',
                        style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.9)),
                    const SizedBox(height: 9),
                    // Réduit plutôt que de déborder (écran étroit, grande taille de texte système).
                    FittedBox(
                      fit: BoxFit.scaleDown,
                      alignment: Alignment.centerLeft,
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.baseline,
                        textBaseline: TextBaseline.alphabetic,
                        children: [
                          Text(
                            _formatRate(overall.winRate),
                            style: AppTheme.mono(
                              fontSize: 35,
                              fontWeight: FontWeight.w700,
                              letterSpacing: -1.5,
                              color: overall.winRate == null ? AppColors.textTertiary : AppColors.textPrimary,
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text('de réussite',
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
                  Text('SIGNAUX', style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.9)),
                  const SizedBox(height: 9),
                  Text(count(overall.total),
                      style: AppTheme.mono(fontSize: 16, fontWeight: FontWeight.w700, color: AppColors.textPrimary)),
                ],
              ),
            ],
          ),
          const SizedBox(height: 14),
          Row(
            children: [
              // Libellés courts (comme sur l'accueil) : « TP ATTEINTS » était tronqué.
              Expanded(child: _MiniStat(label: 'GAGNÉS', value: count(overall.hitTp), color: AppColors.success)),
              const SizedBox(width: 10),
              Expanded(child: _MiniStat(label: 'PERDUS', value: count(overall.hitSl), color: AppColors.danger)),
              const SizedBox(width: 10),
              Expanded(
                  child: _MiniStat(label: 'EXPIRÉS', value: count(overall.expired), color: AppColors.textSecondary)),
            ],
          ),
          const SizedBox(height: 18),
          Text('PAR STRATÉGIE', style: AppTheme.labelMicro().copyWith(fontSize: 11, letterSpacing: 0.8)),
          const SizedBox(height: 12),
          for (final code in strategies) ...[
            _StrategyRateRow(label: signalStrategyLabel(code), stats: report?.byStrategy[code]),
            const SizedBox(height: 12),
          ],
          Text(
            'Taux de réussite = TP atteints / (TP + SL), sur les signaux réellement clôturés ; '
            'les expirés ne sont pas comptés. Les résultats passés ne préjugent pas des résultats futurs.',
            style: GoogleFonts.manrope(fontSize: 11, color: AppColors.textTertiary, height: 1.4),
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
          Text(label, maxLines: 1, overflow: TextOverflow.ellipsis, style: AppTheme.labelMicro()),
          const SizedBox(height: 5),
          Text(value, style: AppTheme.mono(fontSize: 16, fontWeight: FontWeight.w700, color: color)),
        ],
      ),
    );
  }
}

class _StrategyRateRow extends StatelessWidget {
  const _StrategyRateRow({required this.label, required this.stats});
  final String label;
  final SignalStats? stats;

  @override
  Widget build(BuildContext context) {
    final rate = stats?.winRate;
    final decided = stats?.decided ?? 0;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(label,
                  style: GoogleFonts.manrope(fontSize: 12, fontWeight: FontWeight.w700, color: AppColors.textSecondary)),
            ),
            Text(decided == 0 ? 'aucun TP/SL' : 'sur $decided',
                style: AppTheme.mono(fontSize: 10.5, color: AppColors.textTertiary)),
            const SizedBox(width: 10),
            SizedBox(
              width: 46,
              child: Text(
                _formatRate(rate),
                textAlign: TextAlign.right,
                style: AppTheme.mono(
                  fontSize: 12.5,
                  fontWeight: FontWeight.w700,
                  color: rate == null ? AppColors.textTertiary : AppColors.textPrimary,
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 6),
        Container(
          height: 5,
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.06),
            borderRadius: BorderRadius.circular(AppRadii.pill),
          ),
          child: FractionallySizedBox(
            widthFactor: rate ?? 0,
            alignment: Alignment.centerLeft,
            child: Container(
              decoration: BoxDecoration(color: AppColors.primary, borderRadius: BorderRadius.circular(AppRadii.pill)),
            ),
          ),
        ),
      ],
    );
  }
}

class _LockedCard extends StatelessWidget {
  const _LockedCard({required this.onOpenPremium});
  final VoidCallback onOpenPremium;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: AppColors.primary.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.lg + 2),
        border: Border.all(color: AppColors.primary.withValues(alpha: 0.35), width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 38,
                height: 38,
                decoration: BoxDecoration(
                  color: AppColors.primary.withValues(alpha: 0.16),
                  borderRadius: BorderRadius.circular(12),
                ),
                alignment: Alignment.center,
                child: const Icon(Icons.lock_outline_rounded, size: 19, color: AppColors.primarySoft),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Signaux en direct réservés à l\'essai gratuit et au Premium',
                        style: GoogleFonts.manrope(
                            fontSize: 13.5, fontWeight: FontWeight.w800, color: AppColors.textPrimary, height: 1.3)),
                    const SizedBox(height: 4),
                    Text(
                      'Vous voyez uniquement l\'historique des signaux clôturés. Passez au Premium pour '
                      'recevoir les nouveaux signaux en temps réel et leurs notifications.',
                      style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.4),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),
          SizedBox(
            height: 46,
            child: FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: AppColors.primary,
                foregroundColor: Colors.white,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadii.md)),
              ),
              onPressed: onOpenPremium,
              child: Text('Voir les formules Premium',
                  style: GoogleFonts.manrope(fontSize: 14, fontWeight: FontWeight.w800)),
            ),
          ),
        ],
      ),
    );
  }
}

/// Switch segmenté Tous / Actifs / Clôturés.
class _StatusSegments extends StatelessWidget {
  const _StatusSegments({required this.value, required this.onChanged});
  final _StatusFilter value;
  final ValueChanged<_StatusFilter> onChanged;

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
          _seg(_StatusFilter.all, 'Tous'),
          _seg(_StatusFilter.active, 'Actifs'),
          _seg(_StatusFilter.closed, 'Clôturés'),
        ],
      ),
    );
  }

  Widget _seg(_StatusFilter key, String label) {
    final selected = value == key;
    return Expanded(
      child: InkWell(
        onTap: () => onChanged(key),
        borderRadius: BorderRadius.circular(AppRadii.md - 2),
        child: Container(
          height: 38,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected ? AppColors.primary.withValues(alpha: 0.16) : Colors.transparent,
            borderRadius: BorderRadius.circular(AppRadii.md - 2),
            border: Border.all(
              color: selected ? AppColors.primary.withValues(alpha: 0.5) : Colors.transparent,
              width: 1,
            ),
          ),
          child: Text(
            label,
            style: GoogleFonts.manrope(
              fontSize: 13,
              fontWeight: FontWeight.w800,
              color: selected ? AppColors.primarySoft : AppColors.textTertiary,
              letterSpacing: 0.3,
            ),
          ),
        ),
      ),
    );
  }
}

/// Filtre par indice (défilement horizontal).
class _SymbolChips extends StatelessWidget {
  const _SymbolChips({required this.symbols, required this.selected, required this.onChanged});
  final List<String> symbols;
  final String? selected;
  final ValueChanged<String?> onChanged;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(
        children: [
          _chip('TOUS', selected == null, () => onChanged(null)),
          for (final symbol in symbols) ...[
            const SizedBox(width: 8),
            _chip(symbol, selected == symbol, () => onChanged(selected == symbol ? null : symbol)),
          ],
        ],
      ),
    );
  }

  Widget _chip(String label, bool isSelected, VoidCallback onTap) {
    return InkWell(
      borderRadius: BorderRadius.circular(11),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 8),
        decoration: BoxDecoration(
          color: isSelected ? AppColors.primary.withValues(alpha: 0.18) : Colors.white.withValues(alpha: 0.04),
          border: Border.all(
            color: isSelected ? AppColors.primary.withValues(alpha: 0.5) : AppColors.border,
            width: 1,
          ),
          borderRadius: BorderRadius.circular(11),
        ),
        child: Text(
          label,
          style: AppTheme.mono(
            fontSize: 11.5,
            fontWeight: FontWeight.w700,
            color: isSelected ? AppColors.primarySoft : AppColors.textSecondary,
          ),
        ),
      ),
    );
  }
}

class _SignalCard extends StatelessWidget {
  const _SignalCard({super.key, required this.signal});
  final Signal signal;

  @override
  Widget build(BuildContext context) {
    final Color dirColor = signal.isBuy ? AppColors.success : AppColors.danger;
    final (String statusLabel, Color statusColor) = _statusVisual(signal.status);

    return Container(
      padding: const EdgeInsets.fromLTRB(14, 14, 14, 13),
      decoration: AppTheme.card(
        radius: AppRadii.lg,
        border: signal.isActive ? AppColors.border : AppColors.borderSoft,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 34,
                height: 34,
                decoration: BoxDecoration(
                  color: dirColor.withValues(alpha: 0.13),
                  borderRadius: BorderRadius.circular(11),
                ),
                alignment: Alignment.center,
                child: Text(signal.isBuy ? '▲' : '▼',
                    style: GoogleFonts.manrope(color: dirColor, fontSize: 13, fontWeight: FontWeight.w800)),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(signal.displayName,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: GoogleFonts.manrope(
                            fontSize: 13.5, fontWeight: FontWeight.w800, color: AppColors.textPrimary)),
                    const SizedBox(height: 3),
                    Text('${signal.symbol} · ${signal.timeframe} · ${_fmtTime(signal.createdAt)}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: AppTheme.mono(fontSize: 10.5, color: AppColors.textTertiary)),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              StatusPill(label: statusLabel, color: statusColor),
            ],
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              _Tag(text: signal.isBuy ? 'ACHAT' : 'VENTE', color: dirColor),
              const SizedBox(width: 6),
              _Tag(text: signal.strategyLabel, color: AppColors.primarySoft),
              const SizedBox(width: 8),
              Expanded(
                child: Align(
                  alignment: Alignment.centerRight,
                  child: signal.isActive ? _Countdown(expiresAt: signal.expiresAt) : _ClosedInfo(signal: signal),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.035),
              borderRadius: BorderRadius.circular(13),
            ),
            child: Row(
              children: [
                Expanded(child: _PriceCell(label: 'ENTRÉE', value: signal.entry, color: AppColors.textPrimary)),
                Expanded(child: _PriceCell(label: 'SL', value: signal.stopLoss, color: AppColors.danger)),
                Expanded(child: _PriceCell(label: 'TP', value: signal.takeProfit, color: AppColors.success)),
              ],
            ),
          ),
          if (signal.note.isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(signal.note,
                style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.4)),
          ],
        ],
      ),
    );
  }

  static (String, Color) _statusVisual(String status) {
    switch (status) {
      case 'active':
        return ('Actif', AppColors.primarySoft);
      case 'hit_tp':
        return ('TP atteint', AppColors.success);
      case 'hit_sl':
        return ('SL touché', AppColors.danger);
      case 'expired':
        return ('Expiré', AppColors.textTertiary);
      default:
        return (status, AppColors.textTertiary);
    }
  }
}

class _Tag extends StatelessWidget {
  const _Tag({required this.text, required this.color});
  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.13),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(text,
          style: GoogleFonts.manrope(fontSize: 11, fontWeight: FontWeight.w800, color: color, letterSpacing: 0.3)),
    );
  }
}

class _PriceCell extends StatelessWidget {
  const _PriceCell({required this.label, required this.value, required this.color});
  final String label;
  final double value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: AppTheme.labelMicro()),
        const SizedBox(height: 4),
        FittedBox(
          fit: BoxFit.scaleDown,
          alignment: Alignment.centerLeft,
          child: Text(formatSignalPrice(value),
              style: AppTheme.mono(fontSize: 13, fontWeight: FontWeight.w700, color: color)),
        ),
      ],
    );
  }
}

/// Compte à rebours jusqu'à l'expiration d'un signal actif (rafraîchi chaque seconde).
class _Countdown extends StatefulWidget {
  const _Countdown({required this.expiresAt});
  final DateTime? expiresAt;

  @override
  State<_Countdown> createState() => _CountdownState();
}

class _CountdownState extends State<_Countdown> {
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final expiresAt = widget.expiresAt;
    if (expiresAt == null) return const SizedBox.shrink();
    final remaining = expiresAt.difference(DateTime.now());
    final elapsed = remaining <= Duration.zero;
    // Échéance passée : on attend la mise à jour de statut envoyée par le serveur.
    final text = elapsed ? 'Expiration…' : 'Expire dans ${_fmtDuration(remaining)}';
    final color = !elapsed && remaining.inSeconds <= 60 ? AppColors.warning : AppColors.textSecondary;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(Icons.timer_outlined, size: 13, color: color),
        const SizedBox(width: 4),
        Flexible(
          child: Text(text,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: AppTheme.mono(fontSize: 10.5, fontWeight: FontWeight.w700, color: color)),
        ),
      ],
    );
  }
}

class _ClosedInfo extends StatelessWidget {
  const _ClosedInfo({required this.signal});
  final Signal signal;

  @override
  Widget build(BuildContext context) {
    final closedAt = signal.closedAt;
    final closePrice = signal.closePrice;
    if (closedAt == null && closePrice == null) return const SizedBox.shrink();
    final parts = <String>[
      if (closedAt != null) 'Clôturé ${_fmtTime(closedAt)}',
      if (closePrice != null) formatSignalPrice(closePrice),
    ];
    return Text(parts.join(' · '),
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: AppTheme.mono(fontSize: 10.5, color: AppColors.textTertiary));
  }
}

class _MessageCard extends StatelessWidget {
  const _MessageCard({
    required this.icon,
    required this.color,
    required this.title,
    required this.subtitle,
    this.cta,
    this.onTap,
  });

  final IconData icon;
  final Color color;
  final String title;
  final String subtitle;
  final String? cta;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final label = cta;
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: AppTheme.card(radius: AppRadii.lg + 2),
      child: Column(
        children: [
          Icon(icon, size: 26, color: color),
          const SizedBox(height: 10),
          Text(title,
              textAlign: TextAlign.center,
              style: GoogleFonts.manrope(fontSize: 13.5, fontWeight: FontWeight.w800, color: AppColors.textPrimary)),
          const SizedBox(height: 6),
          Text(subtitle,
              textAlign: TextAlign.center,
              style: GoogleFonts.manrope(fontSize: 12, color: AppColors.textTertiary, height: 1.4)),
          if (label != null && onTap != null) ...[
            const SizedBox(height: 10),
            TextButton(
              onPressed: onTap,
              child: Text(label, style: GoogleFonts.manrope(fontSize: 13, fontWeight: FontWeight.w800, color: color)),
            ),
          ],
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
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AppColors.danger.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.sm + 1),
        border: Border.all(color: AppColors.danger.withValues(alpha: 0.3), width: 1),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline_rounded, size: 16, color: AppColors.danger),
          const SizedBox(width: 8),
          Expanded(
            child: Text(message,
                style: GoogleFonts.manrope(fontSize: 11.5, fontWeight: FontWeight.w600, color: AppColors.textSecondary)),
          ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.title, required this.subtitle});
  final String title;
  final String subtitle;

  @override
  Widget build(BuildContext context) {
    return Container(
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
          Text(subtitle,
              textAlign: TextAlign.center,
              style: GoogleFonts.manrope(fontSize: 12, color: AppColors.textTertiary, height: 1.4)),
        ],
      ),
    );
  }
}

String _formatRate(double? rate) => rate == null ? '—' : '${(rate * 100).round()}%';

/// Heure locale « HH:mm », préfixée de la date si ce n'est pas aujourd'hui.
String _fmtTime(DateTime? dt) {
  if (dt == null) return '—';
  final local = dt.toLocal();
  final now = DateTime.now();
  String two(int n) => n.toString().padLeft(2, '0');
  final hm = '${two(local.hour)}:${two(local.minute)}';
  final sameDay = local.year == now.year && local.month == now.month && local.day == now.day;
  return sameDay ? hm : '${two(local.day)}/${two(local.month)} $hm';
}

String _fmtDuration(Duration d) {
  String two(int n) => n.toString().padLeft(2, '0');
  final hours = d.inHours;
  final minutes = d.inMinutes.remainder(60);
  final seconds = d.inSeconds.remainder(60);
  return hours > 0 ? '$hours:${two(minutes)}:${two(seconds)}' : '${two(minutes)}:${two(seconds)}';
}

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/auth_provider.dart';
import '../providers/bot_provider.dart';
import '../services/bot_service.dart';
import 'admin_user_management_screen.dart';
import 'api_token_screen.dart';
import 'login_screen.dart';
import 'require_admin.dart';

/// Tableau de bord : état, PnL, configuration, contrôle START/STOP, trades.
class DashboardScreen extends ConsumerStatefulWidget {
  const DashboardScreen({super.key});

  @override
  ConsumerState<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends ConsumerState<DashboardScreen> {
  final TextEditingController _symbol = TextEditingController(text: 'R_100');
  final TextEditingController _stake = TextEditingController(text: '1.0');
  final TextEditingController _stopLoss = TextEditingController(text: '10.0');
  final TextEditingController _takeProfit = TextEditingController(text: '20.0');
  String _strategy = 'RISE_FALL';
  bool _busy = false;

  static const List<String> _strategies = ['RISE_FALL', 'OVER_UNDER'];
  static const Set<String> _activeStates = {'RUNNING', 'PAUSED'};

  @override
  void dispose() {
    _symbol.dispose();
    _stake.dispose();
    _stopLoss.dispose();
    _takeProfit.dispose();
    super.dispose();
  }

  BotService get _service => ref.read(botServiceProvider);

  double _parse(TextEditingController c, double fallback) =>
      double.tryParse(c.text.trim().replaceAll(',', '.')) ?? fallback;

  Future<void> _start() async {
    final token = ref.read(tokenProvider) ??
        await ref.read(secureStorageProvider).read(key: kTokenKey);
    if (token == null || token.isEmpty) {
      _snack('Token manquant');
      return;
    }
    setState(() => _busy = true);
    try {
      await _service.startBot(
        token: token,
        symbol: _symbol.text.trim(),
        stake: _parse(_stake, 1),
        stopLoss: _parse(_stopLoss, 10),
        takeProfit: _parse(_takeProfit, 20),
        strategy: _strategy,
      );
      _snack('Bot démarré');
    } on BotServiceException catch (e) {
      _snack(e.toString());
    } catch (e) {
      _snack('Échec : $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _stop() async {
    setState(() => _busy = true);
    try {
      await _service.stopBot();
      _snack('Bot arrêté');
    } on BotServiceException catch (e) {
      _snack(e.toString());
    } catch (e) {
      _snack('Échec : $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
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

  @override
  Widget build(BuildContext context) {
    final statusAsync = ref.watch(botStatusStreamProvider);
    final isAdmin = ref.watch(isAdminProvider);
    final isLoggedIn = ref.watch(jwtProvider) != null;
    final Map<String, dynamic> status = statusAsync.maybeWhen(
      data: (data) => data,
      orElse: () => const <String, dynamic>{},
    );

    final String state = (status['state'] as String?) ?? 'STOPPED';
    final double pnl = (status['pnl'] as num?)?.toDouble() ?? 0.0;
    final bool isActive = _activeStates.contains(state);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Deriv Trading Bot'),
        actions: [
          if (isAdmin)
            IconButton(
              icon: const Icon(Icons.admin_panel_settings),
              tooltip: 'Page Admin',
              onPressed: _openAdmin,
            )
          else if (!isLoggedIn)
            IconButton(
              icon: const Icon(Icons.person_outline),
              tooltip: 'Connexion',
              onPressed: _openLogin,
            ),
          IconButton(
            icon: const Icon(Icons.logout),
            tooltip: 'Changer de token',
            onPressed: _logout,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(botStatusStreamProvider),
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _StatusBadge(state: state, connected: statusAsync.hasValue),
            const SizedBox(height: 16),
            _PnlCard(pnl: pnl, status: status),
            const SizedBox(height: 16),
            _ConfigForm(
              symbol: _symbol,
              stake: _stake,
              stopLoss: _stopLoss,
              takeProfit: _takeProfit,
              strategy: _strategy,
              strategies: _strategies,
              enabled: !isActive && !_busy,
              onStrategyChanged: (v) => setState(() => _strategy = v),
            ),
            const SizedBox(height: 16),
            _ControlButton(
              isActive: isActive,
              busy: _busy,
              onStart: _start,
              onStop: _stop,
            ),
            const SizedBox(height: 24),
            const Text('5 derniers trades',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            _TradeList(trades: status['last_trades']),
          ],
        ),
      ),
    );
  }
}

class _StatusBadge extends StatelessWidget {
  const _StatusBadge({required this.state, required this.connected});

  final String state;
  final bool connected;

  Color get _color {
    switch (state) {
      case 'RUNNING':
        return Colors.green;
      case 'STOP_LOSS_REACHED':
      case 'ERROR':
        return Colors.red;
      case 'TAKE_PROFIT_REACHED':
        return Colors.blue;
      case 'PAUSED':
        return Colors.orange;
      default:
        return Colors.grey;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 16),
      decoration: BoxDecoration(
        color: _color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: _color, width: 2),
      ),
      child: Row(
        children: [
          Icon(Icons.circle, color: _color, size: 16),
          const SizedBox(width: 12),
          Text(
            state,
            style: TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.bold,
              color: _color,
            ),
          ),
          const Spacer(),
          Icon(
            connected ? Icons.wifi : Icons.wifi_off,
            color: connected ? Colors.green : Colors.grey,
            size: 18,
          ),
        ],
      ),
    );
  }
}

class _PnlCard extends StatelessWidget {
  const _PnlCard({required this.pnl, required this.status});

  final double pnl;
  final Map<String, dynamic> status;

  @override
  Widget build(BuildContext context) {
    final Color color = pnl >= 0 ? Colors.green : Colors.red;
    final String currency = (status['currency'] as String?) ?? '';
    final double balance =
        (status['current_balance'] as num?)?.toDouble() ?? 0.0;
    final int won = (status['trades_won'] as num?)?.toInt() ?? 0;
    final int lost = (status['trades_lost'] as num?)?.toInt() ?? 0;

    return Card(
      elevation: 2,
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          children: [
            const Text('PnL Session',
                style: TextStyle(color: Colors.grey, fontSize: 14)),
            const SizedBox(height: 4),
            Text(
              '${pnl >= 0 ? '+' : ''}${pnl.toStringAsFixed(2)} $currency',
              style: TextStyle(
                fontSize: 40,
                fontWeight: FontWeight.bold,
                color: color,
              ),
            ),
            const SizedBox(height: 12),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                _stat('Solde', '${balance.toStringAsFixed(2)} $currency'),
                _stat('Gagnés', '$won', Colors.green),
                _stat('Perdus', '$lost', Colors.red),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _stat(String label, String value, [Color? color]) {
    return Column(
      children: [
        Text(label, style: const TextStyle(color: Colors.grey, fontSize: 12)),
        const SizedBox(height: 2),
        Text(value,
            style: TextStyle(fontWeight: FontWeight.bold, color: color)),
      ],
    );
  }
}

class _ConfigForm extends StatelessWidget {
  const _ConfigForm({
    required this.symbol,
    required this.stake,
    required this.stopLoss,
    required this.takeProfit,
    required this.strategy,
    required this.strategies,
    required this.enabled,
    required this.onStrategyChanged,
  });

  final TextEditingController symbol;
  final TextEditingController stake;
  final TextEditingController stopLoss;
  final TextEditingController takeProfit;
  final String strategy;
  final List<String> strategies;
  final bool enabled;
  final ValueChanged<String> onStrategyChanged;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            _field(symbol, 'Symbole (ex: R_100)', TextInputType.text),
            const SizedBox(height: 12),
            _field(stake, 'Mise (Stake)',
                const TextInputType.numberWithOptions(decimal: true)),
            const SizedBox(height: 12),
            _field(stopLoss, 'Daily Stop Loss',
                const TextInputType.numberWithOptions(decimal: true)),
            const SizedBox(height: 12),
            _field(takeProfit, 'Daily Take Profit',
                const TextInputType.numberWithOptions(decimal: true)),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: strategy,
              decoration: const InputDecoration(
                labelText: 'Stratégie',
                border: OutlineInputBorder(),
              ),
              items: strategies
                  .map((s) => DropdownMenuItem(value: s, child: Text(s)))
                  .toList(),
              onChanged: enabled
                  ? (v) {
                      if (v != null) onStrategyChanged(v);
                    }
                  : null,
            ),
          ],
        ),
      ),
    );
  }

  Widget _field(
      TextEditingController c, String label, TextInputType type) {
    return TextField(
      controller: c,
      enabled: enabled,
      keyboardType: type,
      decoration: InputDecoration(
        labelText: label,
        border: const OutlineInputBorder(),
        isDense: true,
      ),
    );
  }
}

class _ControlButton extends StatelessWidget {
  const _ControlButton({
    required this.isActive,
    required this.busy,
    required this.onStart,
    required this.onStop,
  });

  final bool isActive;
  final bool busy;
  final VoidCallback onStart;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 56,
      child: FilledButton.icon(
        onPressed: busy ? null : (isActive ? onStop : onStart),
        icon: busy
            ? const SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(
                    strokeWidth: 2, color: Colors.white),
              )
            : Icon(isActive ? Icons.stop : Icons.play_arrow),
        label: Text(
          isActive ? 'STOP BOT' : 'START BOT',
          style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
        ),
        style: FilledButton.styleFrom(
          backgroundColor: isActive ? Colors.red : Colors.green,
        ),
      ),
    );
  }
}

class _TradeList extends StatelessWidget {
  const _TradeList({required this.trades});

  final dynamic trades;

  @override
  Widget build(BuildContext context) {
    final List<Map<String, dynamic>> items = (trades is List)
        ? (trades as List)
            .whereType<Map<String, dynamic>>()
            .toList(growable: false)
        : const <Map<String, dynamic>>[];

    if (items.isEmpty) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Center(
            child: Text('Aucun trade exécuté',
                style: TextStyle(color: Colors.grey)),
          ),
        ),
      );
    }

    return Column(
      children: items.map(_tradeCard).toList(),
    );
  }

  Widget _tradeCard(Map<String, dynamic> t) {
    final double profit = (t['profit'] as num?)?.toDouble() ?? 0.0;
    final bool won = (t['result'] as String?) == 'won';
    final Color color = won ? Colors.green : Colors.red;
    final String type = (t['contract_type'] as String?) ?? '';
    final String currency = ''; // profit déjà exprimé en devise du compte
    final String time = _fmtTime(t['timestamp']);

    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        leading: Icon(
          won ? Icons.trending_up : Icons.trending_down,
          color: color,
        ),
        title: Text(type.isEmpty ? 'Contrat' : type),
        subtitle: Text('#${t['contract_id'] ?? '-'}  ·  $time'),
        trailing: Text(
          '${profit >= 0 ? '+' : ''}${profit.toStringAsFixed(2)}$currency',
          style: TextStyle(
            color: color,
            fontWeight: FontWeight.bold,
            fontSize: 16,
          ),
        ),
      ),
    );
  }

  String _fmtTime(dynamic epoch) {
    final double? seconds = (epoch as num?)?.toDouble();
    if (seconds == null) return '';
    final dt =
        DateTime.fromMillisecondsSinceEpoch((seconds * 1000).round()).toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(dt.hour)}:${two(dt.minute)}:${two(dt.second)}';
  }
}

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../services/copy_service.dart';
import 'auth_provider.dart';

final copyServiceProvider = Provider<CopyService>((ref) => CopyService());

/// État de l'écran copy trading.
class CopyState {
  const CopyState({
    this.me,
    this.masters = const [],
    this.trades = const [],
    this.loading = false,
    this.error,
  });

  final CopyMe? me;
  final List<MasterInfo> masters;
  final List<CopiedTrade> trades;
  final bool loading;

  /// Dernière erreur de chargement, prête à afficher (null si aucune).
  final String? error;

  /// Suivi en cours (null si l'utilisateur ne copie personne).
  FollowOut? get following => me?.following;

  /// [error] n'est pas conservé : ne pas le passer l'efface.
  CopyState copyWith({
    CopyMe? me,
    List<MasterInfo>? masters,
    List<CopiedTrade>? trades,
    bool? loading,
    String? error,
  }) =>
      CopyState(
        me: me ?? this.me,
        masters: masters ?? this.masters,
        trades: trades ?? this.trades,
        loading: loading ?? this.loading,
        error: error,
      );
}

class CopyNotifier extends StateNotifier<CopyState> {
  CopyNotifier(this._service, this._jwt)
      : super(_jwt == null || _jwt.isEmpty
            ? const CopyState(error: 'Connectez-vous pour accéder au copy trading.')
            : const CopyState(loading: true)) {
    // Premier chargement différé : jamais pendant la construction du provider.
    if (_jwt != null && _jwt.isNotEmpty) Future.microtask(refresh);
  }

  final CopyService _service;
  final String? _jwt;

  /// Numéro de la dernière opération : la réponse d'un chargement dépassé est ignorée.
  int _seq = 0;

  /// Recharge l'état, les maîtres et les trades copiés.
  /// [silent] : pas d'indicateur de chargement, erreurs ignorées, et seule la
  /// partie affichée est rechargée (trades si une copie est en cours, maîtres sinon).
  Future<void> refresh({bool silent = false}) async {
    final jwt = _jwt;
    if (!mounted || jwt == null || jwt.isEmpty) return;
    if (silent && state.loading) return;
    final seq = ++_seq;
    if (!silent) state = state.copyWith(loading: true);
    try {
      final me = await _service.fetchMe(jwt);
      if (!mounted || seq != _seq) return;
      if (!me.enabled) {
        state = CopyState(me: me);
        return;
      }
      final bool following = me.following != null;
      final results = await Future.wait<Object>([
        silent && following ? Future.value(state.masters) : _service.fetchMasters(jwt),
        silent && !following ? Future.value(state.trades) : _service.fetchTrades(jwt),
      ]);
      if (!mounted || seq != _seq) return;
      state = CopyState(
        me: me,
        masters: results[0] as List<MasterInfo>,
        trades: results[1] as List<CopiedTrade>,
      );
    } on CopyServiceException catch (e) {
      if (!mounted || seq != _seq || silent) return;
      state = state.copyWith(loading: false, error: e.userMessage());
    } catch (_) {
      // Réponse mal formée : message neutre, sans détail technique.
      if (!mounted || seq != _seq || silent) return;
      state = state.copyWith(loading: false, error: 'Réponse inattendue du serveur, réessayez.');
    }
  }

  /// Commence à copier un maître. Lève [CopyServiceException] (402, 400, 404, 409, 422, 503…).
  Future<FollowOut> follow({
    required int masterId,
    required String apiToken,
    required String accountType,
    required double multiplier,
    required double maxStake,
    required double dailyStopLoss,
    required bool consent,
  }) async {
    final result = await _service.follow(
      _requireJwt(),
      masterId: masterId,
      apiToken: apiToken,
      accountType: accountType,
      multiplier: multiplier,
      maxStake: maxStake,
      dailyStopLoss: dailyStopLoss,
      consent: consent,
    );
    _applyFollowing(result);
    unawaited(refresh(silent: true)); // trades et compteurs à jour
    return result;
  }

  /// Modifie les réglages du suivi (seuls les champs non nuls sont envoyés).
  Future<FollowOut> update({
    double? multiplier,
    double? maxStake,
    double? dailyStopLoss,
    bool? active,
  }) async {
    final result = await _service.updateFollow(
      _requireJwt(),
      multiplier: multiplier,
      maxStake: maxStake,
      dailyStopLoss: dailyStopLoss,
      active: active,
    );
    _applyFollowing(result);
    return result;
  }

  /// Arrête la copie ; le serveur supprime aussi le token chiffré.
  Future<void> unfollow() async {
    try {
      await _service.unfollow(_requireJwt());
    } on CopyServiceException catch (e) {
      // 404 : plus aucun suivi côté serveur, l'état voulu est déjà atteint.
      if (e.statusCode != 404) rethrow;
    }
    _seq++;
    if (!mounted) return;
    state = state.copyWith(me: state.me?.withFollowing(null), trades: const [], loading: false);
    unawaited(refresh(silent: true)); // liste des maîtres (compteurs d'abonnés)
  }

  void _applyFollowing(FollowOut? value) {
    _seq++; // invalide un chargement lancé avant la modification
    if (!mounted) return;
    state = state.copyWith(me: state.me?.withFollowing(value), loading: false);
  }

  String _requireJwt() {
    final jwt = _jwt;
    if (jwt == null || jwt.isEmpty) {
      throw CopyServiceException(401, 'Session expirée');
    }
    return jwt;
  }
}

/// État copy trading, recréé (et rechargé) à chaque changement de session.
final copyProvider = StateNotifierProvider.autoDispose<CopyNotifier, CopyState>((ref) {
  return CopyNotifier(ref.watch(copyServiceProvider), ref.watch(jwtProvider));
});

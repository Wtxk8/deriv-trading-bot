import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../services/notification_service.dart';
import '../services/signal_service.dart';
import 'auth_provider.dart';

/// Nombre maximal de signaux conservés en mémoire (plus récents d'abord).
const int kMaxSignals = 100;

/// Fenêtre (jours) de la carte de performance.
const int kSignalStatsDays = 7;

final signalServiceProvider = Provider<SignalService>((ref) => SignalService());

final notificationServiceProvider =
    Provider<NotificationService>((ref) => NotificationService.instance);

/// État de l'écran Signaux.
class SignalsState {
  const SignalsState({
    this.liveAccess = false,
    this.accessKnown = false,
    this.signals = const <Signal>[],
    this.stats,
    this.loading = false,
    this.error,
  });

  /// Essai actif, Premium actif ou admin : signaux en cours visibles.
  final bool liveAccess;

  /// Droits confirmés par le serveur (GET /signals ou `hello`). Tant que false,
  /// [liveAccess] n'est qu'une valeur par défaut : ne pas afficher le verrou.
  final bool accessKnown;

  /// Plus récents d'abord, au plus [kMaxSignals].
  final List<Signal> signals;

  final SignalStatsReport? stats;
  final bool loading;
  final String? error;

  SignalsState copyWith({
    bool? liveAccess,
    bool? accessKnown,
    List<Signal>? signals,
    SignalStatsReport? stats,
    bool? loading,
    String? error,
    bool clearError = false,
  }) {
    return SignalsState(
      liveAccess: liveAccess ?? this.liveAccess,
      accessKnown: accessKnown ?? this.accessKnown,
      signals: signals ?? this.signals,
      stats: stats ?? this.stats,
      loading: loading ?? this.loading,
      error: clearError ? null : (error ?? this.error),
    );
  }
}

/// Chargement REST puis application du flux WebSocket (insertion / remplacement par id).
class SignalsNotifier extends StateNotifier<SignalsState> {
  SignalsNotifier(this._service, this._notifications, String? jwt)
      : _jwt = jwt ?? '',
        super(const SignalsState()) {
    if (_jwt.isEmpty) return; // sans JWT : état vide, aucune connexion
    state = const SignalsState(loading: true);
    unawaited(_start());
  }

  final SignalService _service;
  final NotificationService _notifications;
  final String _jwt;

  StreamSubscription<SignalEvent>? _subscription;
  Timer? _statsDebounce;
  Future<void>? _refreshing;

  /// Événements reçus avant l'instantané REST initial : appliqués ensuite.
  final List<SignalEvent> _pending = <SignalEvent>[];
  bool _snapshotApplied = false;

  /// Ids déjà notifiés (une notification par signal, même après reconnexion).
  final Set<int> _notified = <int>{};

  int _helloCount = 0;
  bool _streamEnded = false;
  bool? _streamLiveAccess; // droits annoncés par le dernier `hello`
  bool? _restLiveAccess; // droits renvoyés par le dernier GET /signals
  bool _rightsRestartDone = false;

  Future<void> _start() async {
    // Le WebSocket s'ouvre tout de suite ; ses événements attendent l'instantané REST.
    _listen();
    await refresh();
    if (!mounted) return;
    _snapshotApplied = true;
    final pending = List<SignalEvent>.of(_pending);
    _pending.clear();
    pending.forEach(_apply);
  }

  /// Recharge la liste et les statistiques (pull-to-refresh, retour de l'écran Premium).
  Future<void> refresh({bool silent = false}) {
    if (_jwt.isEmpty) return Future<void>.value();
    return _refreshing ??= _load(silent: silent).whenComplete(() => _refreshing = null);
  }

  Future<void> _load({required bool silent}) async {
    if (!silent) state = state.copyWith(loading: true);
    final statsDone = _loadStats();
    try {
      final feed = await _service.fetchSignals(_jwt, limit: 50);
      if (!mounted) return;
      _restLiveAccess = feed.liveAccess;
      state = state.copyWith(
        liveAccess: feed.liveAccess,
        accessKnown: true,
        signals: _mergeSnapshot(feed.signals, liveAccess: feed.liveAccess),
        loading: false,
        clearError: true,
      );
      _syncStreamRights(feed.liveAccess);
    } catch (e) {
      if (!mounted) return;
      state = state.copyWith(loading: false, error: _messageFor(e));
    }
    await statsDone;
  }

  Future<void> _loadStats() async {
    try {
      final stats = await _service.fetchStats(_jwt, days: kSignalStatsDays);
      if (mounted) state = state.copyWith(stats: stats);
    } catch (_) {
      // Statistiques indisponibles : la carte de performance affiche « — ».
    }
  }

  void _listen() {
    unawaited(_subscription?.cancel());
    _helloCount = 0;
    _streamEnded = false;
    _subscription = _service.connectSignalStream(_jwt).listen(
      (event) {
        if (!_snapshotApplied) {
          _pending.add(event);
          return;
        }
        _apply(event);
      },
      onError: (Object error) {
        if (mounted) state = state.copyWith(error: _messageFor(error));
      },
      onDone: () => _streamEnded = true,
    );
  }

  /// Le WebSocket fixe ses droits à la connexion : on le rouvre si le REST en annonce
  /// d'autres (ex. Premium acheté entre-temps), ou s'il s'est arrêté (4401).
  void _syncStreamRights(bool liveAccess) {
    if (_streamEnded) {
      _listen();
      return;
    }
    final streamRights = _streamLiveAccess;
    if (streamRights != null && streamRights != liveAccess && !_rightsRestartDone) {
      _rightsRestartDone = true; // une seule tentative tant que REST et WS divergent
      _listen();
    }
  }

  void _apply(SignalEvent event) {
    if (!mounted) return;
    switch (event) {
      case SignalHello(:final liveAccess):
        _helloCount++;
        _streamLiveAccess = liveAccess;
        if (liveAccess == _restLiveAccess) _rightsRestartDone = false;
        final gained = liveAccess && !state.liveAccess;
        state = state.copyWith(liveAccess: liveAccess, accessKnown: true);
        // Reconnexion ou accès au direct obtenu : rattrapage des signaux manqués.
        if (_helloCount > 1 || gained) unawaited(refresh(silent: true));
      case SignalCreated(:final signal):
        _upsert(signal);
        if (signal.isActive && _notified.add(signal.id)) {
          unawaited(_notifications.showSignal(signal));
        }
      case SignalUpdated(:final signal):
        _upsert(signal);
        if (signal.isClosed) _scheduleStatsRefresh();
    }
  }

  void _upsert(Signal signal) {
    Signal? existing;
    final next = <Signal>[];
    for (final s in state.signals) {
      if (s.id == signal.id) {
        existing = s;
      } else {
        next.add(s);
      }
    }
    next.add(_fresher(signal, existing));
    state = state.copyWith(signals: _sorted(next));
  }

  /// Fusionne un instantané REST avec l'état courant sans perdre les événements
  /// arrivés par le flux pendant la requête.
  List<Signal> _mergeSnapshot(List<Signal> snapshot, {required bool liveAccess}) {
    final current = <int, Signal>{for (final s in state.signals) s.id: s};
    final ids = <int>{};
    var minId = 0;
    final merged = <Signal>[];
    for (final s in snapshot) {
      if (!ids.add(s.id)) continue;
      if (minId == 0 || s.id < minId) minId = s.id;
      merged.add(_fresher(s, current[s.id]));
    }
    for (final s in state.signals) {
      if (ids.contains(s.id)) continue;
      // Hors instantané : on garde ce qui est plus récent que lui (et visible avec ces droits).
      if ((liveAccess || s.isClosed) && s.id > minId) merged.add(s);
    }
    return _sorted(merged);
  }

  /// Cycle de vie monotone (actif → clos) : une version close l'emporte sur une active.
  static Signal _fresher(Signal incoming, Signal? existing) {
    if (existing != null && existing.isClosed && incoming.isActive) return existing;
    return incoming;
  }

  static List<Signal> _sorted(List<Signal> signals) {
    int key(Signal s) => s.createdAt?.millisecondsSinceEpoch ?? 0;
    signals.sort((a, b) {
      final byDate = key(b).compareTo(key(a));
      return byDate != 0 ? byDate : b.id.compareTo(a.id);
    });
    final kept = signals.length > kMaxSignals ? signals.sublist(0, kMaxSignals) : signals;
    return List<Signal>.unmodifiable(kept);
  }

  void _scheduleStatsRefresh() {
    _statsDebounce?.cancel();
    _statsDebounce = Timer(const Duration(seconds: 3), () => unawaited(_loadStats()));
  }

  static String _messageFor(Object error) {
    if (error is SignalServiceException) return error.detail;
    if (error is TimeoutException) return 'Le serveur ne répond pas. Réessayez.';
    return 'Connexion au serveur impossible. Vérifiez votre réseau.';
  }

  @override
  void dispose() {
    _statsDebounce?.cancel();
    unawaited(_subscription?.cancel());
    super.dispose();
  }
}

/// Signaux, statistiques et flux temps réel — recréé à chaque changement de JWT.
///
/// Volontairement non autoDispose : une fois activé, le flux continue d'alimenter
/// les notifications locales pendant la navigation dans l'app.
final signalsProvider = StateNotifierProvider<SignalsNotifier, SignalsState>((ref) {
  return SignalsNotifier(
    ref.watch(signalServiceProvider),
    ref.watch(notificationServiceProvider),
    ref.watch(jwtProvider),
  );
});

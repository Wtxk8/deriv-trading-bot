import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

/// Code de fermeture WebSocket envoyé par le serveur quand le JWT est refusé.
const int kWsAuthRejectedCode = 4401;

/// Fermeture normale côté client (RFC 6455).
const int _normalClosure = 1000;

/// Stratégies de génération connues du backend, dans l'ordre d'affichage.
const List<String> kSignalStrategies = ['MA_CROSS', 'RSI', 'SPIKE'];

/// Libellé français d'une stratégie de signal.
String signalStrategyLabel(String code) => switch (code) {
      'MA_CROSS' => 'Croisement MM',
      'RSI' => 'RSI',
      'SPIKE' => 'Spike',
      _ => code,
    };

/// Indices suivis par défaut (repli si le serveur ne fournit pas son catalogue).
const Map<String, String> kDefaultSignalSymbolNames = {
  'R_75': 'Volatility 75 Index',
  'R_100': 'Volatility 100 Index',
  'BOOM1000': 'Boom 1000 Index',
  'CRASH1000': 'Crash 1000 Index',
  'BOOM500': 'Boom 500 Index',
  'CRASH500': 'Crash 500 Index',
};

/// Formate un prix : 2 à 4 décimales, sans zéros superflus (5913.279, 1234.50).
String formatSignalPrice(double value) {
  if (!value.isFinite) return '—';
  var text = value.toStringAsFixed(4);
  final dot = text.indexOf('.');
  while (dot >= 0 && text.length - dot - 1 > 2 && text.endsWith('0')) {
    text = text.substring(0, text.length - 1);
  }
  return text;
}

/// Client REST + WebSocket des signaux (backend FastAPI via tunnel Cloudflare).
class SignalService {
  SignalService({
    this.host = 'api1.innovahub226.com',
    this.secure = true,
    this.minBackoff = const Duration(seconds: 2),
    this.maxBackoff = const Duration(seconds: 30),
  });

  /// Hôte sans schéma (routé via le tunnel Cloudflare) ; « hôte:port » accepté.
  final String host;

  /// https/wss en production ; false réservé aux tests contre un serveur local.
  final bool secure;

  /// Bornes du backoff de reconnexion du WebSocket (2 s → 30 s par défaut).
  final Duration minBackoff;
  final Duration maxBackoff;

  static const Duration _requestTimeout = Duration(seconds: 15);
  static const Duration _connectTimeout = Duration(seconds: 15);

  Uri _http(String path, [Map<String, String>? query]) =>
      secure ? Uri.https(host, path, query) : Uri.http(host, path, query);
  Uri _ws(String path) => Uri.parse('${secure ? 'wss' : 'ws'}://$host$path');

  Map<String, String> _headers(String jwt) => {
        'Accept': 'application/json',
        'Authorization': 'Bearer $jwt',
      };

  /// GET /signals?limit=N — sans accès au direct, le serveur ne renvoie que les signaux clôturés.
  Future<SignalFeed> fetchSignals(String jwt, {int limit = 50}) async {
    final response = await http
        .get(_http('/signals', {'limit': '$limit'}), headers: _headers(jwt))
        .timeout(_requestTimeout);
    return SignalFeed.fromJson(_decode(response));
  }

  /// GET /signals/stats?days=N — performance réelle calculée côté serveur.
  Future<SignalStatsReport> fetchStats(String jwt, {int days = 7}) async {
    final response = await http
        .get(_http('/signals/stats', {'days': '$days'}), headers: _headers(jwt))
        .timeout(_requestTimeout);
    final body = _decode(response);
    if (body is! Map) {
      throw const SignalServiceException(200, 'Réponse invalide du serveur');
    }
    return SignalStatsReport.fromJson(Map<String, dynamic>.from(body));
  }

  /// GET /signals/preferences — indices et stratégies suivis par l'utilisateur.
  Future<SignalPreferences> fetchPreferences(String jwt) async {
    final response = await http
        .get(_http('/signals/preferences'), headers: _headers(jwt))
        .timeout(_requestTimeout);
    return _preferencesFrom(_decode(response));
  }

  /// PUT /signals/preferences — renvoie les préférences normalisées par le serveur.
  Future<SignalPreferences> updatePreferences(
    String jwt, {
    required List<String> symbols,
    required List<String> strategies,
    required bool notify,
  }) async {
    final response = await http
        .put(
          _http('/signals/preferences'),
          headers: {..._headers(jwt), 'Content-Type': 'application/json'},
          body: jsonEncode(<String, dynamic>{
            'symbols': symbols,
            'strategies': strategies,
            'notify': notify,
          }),
        )
        .timeout(_requestTimeout);
    return _preferencesFrom(_decode(response));
  }

  static SignalPreferences _preferencesFrom(dynamic body) {
    if (body is! Map) {
      throw const SignalServiceException(200, 'Réponse invalide du serveur');
    }
    return SignalPreferences.fromJson(Map<String, dynamic>.from(body));
  }

  /// Flux temps réel de /ws/signals.
  ///
  /// Après connexion, le premier message envoyé est `{"type":"auth","token":jwt}`.
  /// Reconnexion automatique avec backoff 2 s → 30 s (réarmé à chaque `hello`).
  /// Si le serveur ferme en 4401 (JWT refusé), le flux émet une
  /// [SignalServiceException] puis se termine, sans reboucler.
  /// Annuler l'abonnement ferme proprement la connexion.
  Stream<SignalEvent> connectSignalStream(String jwt) => _SignalSocket(
        uri: _ws('/ws/signals'),
        jwt: jwt,
        minBackoff: minBackoff,
        maxBackoff: maxBackoff,
      ).stream;

  dynamic _decode(http.Response response) {
    dynamic body;
    try {
      // bodyBytes + utf8 : FastAPI n'annonce pas de charset, `response.body` décoderait en latin1.
      body = response.bodyBytes.isEmpty ? null : jsonDecode(utf8.decode(response.bodyBytes));
    } on FormatException {
      body = null; // page HTML d'erreur (Cloudflare) ou encodage invalide
    }
    if (response.statusCode >= 400) {
      throw SignalServiceException(response.statusCode, _detailFor(response.statusCode, body));
    }
    if (body == null) {
      throw SignalServiceException(response.statusCode, 'Réponse invalide du serveur');
    }
    return body;
  }

  static String _detailFor(int statusCode, dynamic body) {
    if (statusCode == 401) return 'Session expirée : reconnectez-vous.';
    final detail = body is Map ? body['detail'] : null;
    if (detail is String && detail.trim().isNotEmpty) return detail;
    // 422 de validation FastAPI : liste d'erreurs techniques, peu lisible telle quelle.
    if (detail is List) return 'Données refusées par le serveur. Vérifiez vos choix.';
    if (detail != null) return detail.toString();
    return 'Requête refusée par le serveur';
  }
}

/// Connexion WebSocket unique, reconnectée automatiquement jusqu'à l'annulation.
class _SignalSocket {
  _SignalSocket({
    required this.uri,
    required this.jwt,
    required this.minBackoff,
    required this.maxBackoff,
  }) : _backoff = minBackoff {
    _controller = StreamController<SignalEvent>(
      onListen: () => unawaited(_connect()),
      onCancel: _shutdown,
    );
  }

  final Uri uri;
  final String jwt;
  final Duration minBackoff;
  final Duration maxBackoff;
  late final StreamController<SignalEvent> _controller;

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _subscription;
  Timer? _retryTimer;
  Duration _backoff;
  bool _stopped = false;

  Stream<SignalEvent> get stream => _controller.stream;

  Future<void> _connect() async {
    if (_stopped) return;
    final WebSocketChannel channel;
    try {
      channel = WebSocketChannel.connect(uri);
    } catch (_) {
      // Échec synchrone (plateforme, URI) : même traitement qu'un serveur injoignable.
      _scheduleReconnect();
      return;
    }
    _channel = channel;
    try {
      await channel.ready.timeout(SignalService._connectTimeout);
    } catch (_) {
      // Serveur injoignable ou handshake refusé : nouvelle tentative plus tard.
      if (identical(_channel, channel)) _channel = null;
      _closeQuietly(channel);
      _scheduleReconnect();
      return;
    }
    if (_stopped || !identical(_channel, channel)) {
      _closeQuietly(channel);
      return;
    }
    _subscription = channel.stream.listen(
      _onMessage,
      onError: (Object _) {}, // la fermeture qui suit (onDone) gère la reconnexion
      onDone: () => _onDone(channel),
    );
    // Protocole : le premier message doit être l'authentification.
    channel.sink.add(jsonEncode(<String, String>{'type': 'auth', 'token': jwt}));
  }

  void _onMessage(dynamic raw) {
    if (_stopped || raw is! String) return;
    final message = _decodeMap(raw);
    if (message == null) return;
    switch (message['type']) {
      case 'hello':
        // Connexion authentifiée : le backoff repart de 2 s à la prochaine coupure.
        _backoff = minBackoff;
        final preferences = message['preferences'];
        final prefs = preferences is Map ? preferences : const <String, dynamic>{};
        _controller.add(SignalHello(
          liveAccess: _toBool(message['live_access']),
          symbols: prefs['symbols'] is List ? _codeList(prefs['symbols']) : null,
          strategies: prefs['strategies'] is List ? _codeList(prefs['strategies']) : null,
          notify: prefs.containsKey('notify') ? _toBool(prefs['notify']) : null,
        ));
      case 'signal':
        final signal = Signal.tryParse(message['signal']);
        if (signal != null) _controller.add(SignalCreated(signal));
      case 'update':
        final signal = Signal.tryParse(message['signal']);
        if (signal != null) _controller.add(SignalUpdated(signal));
      default:
        // auth_ok, ping, types inconnus : ignorés.
        break;
    }
  }

  void _onDone(WebSocketChannel channel) {
    if (!identical(_channel, channel)) return;
    _channel = null;
    _subscription = null;
    if (_stopped) return;
    if (channel.closeCode == kWsAuthRejectedCode) {
      // JWT refusé : inutile de reboucler, la session doit être renouvelée.
      _stopped = true;
      _controller.addError(
        const SignalServiceException(kWsAuthRejectedCode, 'Session expirée : reconnectez-vous.'),
      );
      unawaited(_controller.close());
      return;
    }
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_stopped) return;
    final delay = _backoff;
    final next = _backoff * 2;
    _backoff = next > maxBackoff ? maxBackoff : next;
    _retryTimer?.cancel();
    _retryTimer = Timer(delay, () => unawaited(_connect()));
  }

  Future<void> _shutdown() async {
    _stopped = true;
    _retryTimer?.cancel();
    _retryTimer = null;
    final channel = _channel;
    final subscription = _subscription;
    _channel = null;
    _subscription = null;
    if (channel != null) _closeQuietly(channel);
    await subscription?.cancel();
  }

  static void _closeQuietly(WebSocketChannel channel) {
    try {
      unawaited(Future<void>.value(channel.sink.close(_normalClosure)).catchError((Object _) {}));
    } catch (_) {
      // Déjà fermé : rien à faire.
    }
  }

  static Map<String, dynamic>? _decodeMap(String raw) {
    try {
      final decoded = jsonDecode(raw);
      return decoded is Map ? Map<String, dynamic>.from(decoded) : null;
    } on FormatException {
      return null;
    }
  }
}

/// Événement du flux temps réel /ws/signals.
sealed class SignalEvent {
  const SignalEvent();
}

/// Premier message après authentification : droit d'accès aux signaux en direct
/// et préférences en vigueur (champs null si le serveur ne les a pas transmis).
final class SignalHello extends SignalEvent {
  const SignalHello({required this.liveAccess, this.symbols, this.strategies, this.notify});
  final bool liveAccess;
  final List<String>? symbols;
  final List<String>? strategies;
  final bool? notify;
}

/// Nouveau signal publié (envoyé uniquement aux comptes ayant l'accès au direct).
final class SignalCreated extends SignalEvent {
  const SignalCreated(this.signal);
  final Signal signal;
}

/// Changement de statut d'un signal (TP atteint, SL touché, expiré…).
final class SignalUpdated extends SignalEvent {
  const SignalUpdated(this.signal);
  final Signal signal;
}

/// Signal de trading tel que publié par le backend (SignalOut).
class Signal {
  const Signal({
    required this.id,
    required this.symbol,
    required this.symbolName,
    required this.strategy,
    required this.direction,
    required this.entry,
    required this.stopLoss,
    required this.takeProfit,
    required this.timeframe,
    required this.createdAt,
    required this.expiresAt,
    required this.status,
    required this.closedAt,
    required this.closePrice,
    required this.note,
  });

  final int id;
  final String symbol;
  final String symbolName;
  final String strategy; // MA_CROSS | RSI | SPIKE
  final String direction; // BUY | SELL
  final double entry;
  final double stopLoss;
  final double takeProfit;
  final String timeframe;
  final DateTime? createdAt;
  final DateTime? expiresAt;
  final String status; // active | hit_tp | hit_sl | expired
  final DateTime? closedAt;
  final double? closePrice;
  final String note;

  bool get isActive => status == 'active';
  bool get isClosed => !isActive;
  bool get isBuy => direction == 'BUY';

  /// Nom lisible de l'indice, avec repli sur le code.
  String get displayName => symbolName.trim().isEmpty ? symbol : symbolName;

  String get strategyLabel => signalStrategyLabel(strategy);

  factory Signal.fromJson(Map<String, dynamic> json) {
    final symbol = _toStr(json['symbol']).trim();
    return Signal(
      id: _toInt(json['id']) ?? 0,
      symbol: symbol,
      symbolName: _toStr(json['symbol_name']).trim(),
      strategy: _toStr(json['strategy']).trim().toUpperCase(),
      direction: _toStr(json['direction']).trim().toUpperCase(),
      entry: _toDouble(json['entry']) ?? 0,
      stopLoss: _toDouble(json['stop_loss']) ?? 0,
      takeProfit: _toDouble(json['take_profit']) ?? 0,
      timeframe: _toStr(json['timeframe'], '1m'),
      createdAt: _toDate(json['created_at']),
      expiresAt: _toDate(json['expires_at']),
      status: _toStr(json['status'], 'active').trim().toLowerCase(),
      closedAt: _toDate(json['closed_at']),
      closePrice: _toDouble(json['close_price']),
      note: _toStr(json['note']).trim(),
    );
  }

  /// Variante sans exception : null si la charge utile n'est pas un signal exploitable.
  static Signal? tryParse(dynamic raw) {
    if (raw is! Map) return null;
    try {
      final signal = Signal.fromJson(Map<String, dynamic>.from(raw));
      return signal.id > 0 ? signal : null;
    } catch (_) {
      return null;
    }
  }
}

/// Réponse de GET /signals.
class SignalFeed {
  const SignalFeed({required this.liveAccess, required this.signals});

  final bool liveAccess;
  final List<Signal> signals;

  factory SignalFeed.fromJson(dynamic body) {
    if (body is Map) {
      return SignalFeed(
        liveAccess: _toBool(body['live_access']),
        signals: _parseSignals(body['signals']),
      );
    }
    // Tolère une liste nue (sans enveloppe) : accès direct inconnu → refusé.
    return SignalFeed(liveAccess: false, signals: _parseSignals(body));
  }

  static List<Signal> _parseSignals(dynamic raw) {
    if (raw is! List) return const <Signal>[];
    return raw.map(Signal.tryParse).whereType<Signal>().toList(growable: false);
  }
}

/// Compteurs de performance sur une fenêtre glissante.
class SignalStats {
  const SignalStats({
    this.total = 0,
    this.hitTp = 0,
    this.hitSl = 0,
    this.expired = 0,
    this.winRate,
  });

  static const SignalStats empty = SignalStats();

  final int total;
  final int hitTp;
  final int hitSl;
  final int expired;

  /// Ratio 0..1 = hit_tp / (hit_tp + hit_sl) ; null tant qu'aucun signal n'a touché TP ou SL.
  final double? winRate;

  /// Signaux tranchés (TP ou SL), base du taux de réussite.
  int get decided => hitTp + hitSl;

  factory SignalStats.fromJson(Map<String, dynamic> json) {
    final hitTp = _toInt(json['hit_tp']) ?? 0;
    final hitSl = _toInt(json['hit_sl']) ?? 0;
    var rate = _toDouble(json['win_rate']);
    if (rate != null && rate > 1) rate = rate / 100; // tolère un pourcentage
    if (rate == null && hitTp + hitSl > 0) rate = hitTp / (hitTp + hitSl);
    return SignalStats(
      total: _toInt(json['total']) ?? 0,
      hitTp: hitTp,
      hitSl: hitSl,
      expired: _toInt(json['expired']) ?? 0,
      winRate: rate?.clamp(0.0, 1.0).toDouble(),
    );
  }
}

/// Réponse de GET /signals/stats.
class SignalStatsReport {
  const SignalStatsReport({
    required this.windowDays,
    required this.overall,
    required this.byStrategy,
    required this.bySymbol,
  });

  final int windowDays;
  final SignalStats overall;
  final Map<String, SignalStats> byStrategy;
  final Map<String, SignalStats> bySymbol;

  factory SignalStatsReport.fromJson(Map<String, dynamic> json) {
    final overall = json['overall'];
    return SignalStatsReport(
      windowDays: _toInt(json['window_days']) ?? 7,
      overall: overall is Map ? SignalStats.fromJson(Map<String, dynamic>.from(overall)) : SignalStats.empty,
      byStrategy: _group(json['by_strategy'], 'strategy', upperCase: true),
      bySymbol: _group(json['by_symbol'], 'symbol'),
    );
  }

  static Map<String, SignalStats> _group(dynamic raw, String keyField, {bool upperCase = false}) {
    final out = <String, SignalStats>{};
    void put(dynamic key, dynamic value) {
      if (value is! Map) return;
      var k = _toStr(key).trim();
      if (k.isEmpty) return;
      if (upperCase) k = k.toUpperCase();
      out[k] = SignalStats.fromJson(Map<String, dynamic>.from(value));
    }

    if (raw is List) {
      for (final item in raw) {
        if (item is Map) put(item[keyField], item);
      }
    } else if (raw is Map) {
      // Tolère la forme {"RSI": {...}}.
      raw.forEach(put);
    }
    return out;
  }
}

/// Indice proposé dans les préférences.
class SignalSymbolOption {
  const SignalSymbolOption({required this.symbol, required this.name});
  final String symbol;
  final String name;
}

/// Stratégie proposée dans les préférences.
class SignalStrategyOption {
  const SignalStrategyOption({required this.key, required this.label});
  final String key;
  final String label;
}

/// Choix de l'utilisateur : indices et stratégies suivis, notifications (GET/PUT /signals/preferences).
class SignalPreferences {
  const SignalPreferences({
    required this.symbols,
    required this.strategies,
    required this.notify,
    required this.availableSymbols,
    required this.availableStrategies,
  });

  final List<String> symbols;
  final List<String> strategies;
  final bool notify;
  final List<SignalSymbolOption> availableSymbols;
  final List<SignalStrategyOption> availableStrategies;

  /// Champ absent : tous les indices et stratégies disponibles, notifications actives.
  factory SignalPreferences.fromJson(Map<String, dynamic> json) {
    final symbolsCatalog = _symbolOptions(json['available_symbols']);
    final strategiesCatalog = _strategyOptions(json['available_strategies']);
    return SignalPreferences(
      symbols: json['symbols'] is List
          ? _codeList(json['symbols'])
          : List.unmodifiable([for (final o in symbolsCatalog) o.symbol]),
      strategies: json['strategies'] is List
          ? _codeList(json['strategies'])
          : List.unmodifiable([for (final o in strategiesCatalog) o.key]),
      notify: json.containsKey('notify') ? _toBool(json['notify']) : true,
      availableSymbols: symbolsCatalog,
      availableStrategies: strategiesCatalog,
    );
  }

  /// Le signal porte-t-il sur un indice ET une stratégie choisis ?
  bool matches(Signal signal) =>
      symbols.contains(signal.symbol.toUpperCase()) && strategies.contains(signal.strategy.toUpperCase());

  SignalPreferences copyWith({List<String>? symbols, List<String>? strategies, bool? notify}) {
    return SignalPreferences(
      symbols: symbols ?? this.symbols,
      strategies: strategies ?? this.strategies,
      notify: notify ?? this.notify,
      availableSymbols: availableSymbols,
      availableStrategies: availableStrategies,
    );
  }

  static List<SignalSymbolOption> _symbolOptions(dynamic raw) {
    final out = <SignalSymbolOption>[];
    final seen = <String>{};
    if (raw is List) {
      for (final item in raw) {
        final map = item is Map ? item : null;
        final symbol = _toStr(map != null ? map['symbol'] : item).trim().toUpperCase();
        if (symbol.isEmpty || !seen.add(symbol)) continue;
        final name = _toStr(map?['name']).trim();
        out.add(SignalSymbolOption(
          symbol: symbol,
          name: name.isNotEmpty ? name : (kDefaultSignalSymbolNames[symbol] ?? symbol),
        ));
      }
    }
    if (out.isNotEmpty) return List.unmodifiable(out);
    return List.unmodifiable([
      for (final e in kDefaultSignalSymbolNames.entries) SignalSymbolOption(symbol: e.key, name: e.value),
    ]);
  }

  static List<SignalStrategyOption> _strategyOptions(dynamic raw) {
    final out = <SignalStrategyOption>[];
    final seen = <String>{};
    if (raw is List) {
      for (final item in raw) {
        final map = item is Map ? item : null;
        final key = _toStr(map != null ? map['key'] : item).trim().toUpperCase();
        if (key.isEmpty || !seen.add(key)) continue;
        final label = _toStr(map?['label']).trim();
        out.add(SignalStrategyOption(key: key, label: label.isNotEmpty ? label : signalStrategyLabel(key)));
      }
    }
    if (out.isNotEmpty) return List.unmodifiable(out);
    return List.unmodifiable([
      for (final key in kSignalStrategies) SignalStrategyOption(key: key, label: signalStrategyLabel(key)),
    ]);
  }
}

class SignalServiceException implements Exception {
  const SignalServiceException(this.statusCode, this.detail);

  final int statusCode;
  final String detail;

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

// --- Conversions tolérantes -------------------------------------------------

String _toStr(dynamic value, [String fallback = '']) {
  if (value == null) return fallback;
  return value is String ? value : value.toString();
}

int? _toInt(dynamic value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) {
    final text = value.trim();
    return int.tryParse(text) ?? double.tryParse(text)?.toInt();
  }
  return null;
}

double? _toDouble(dynamic value) {
  if (value is num) return value.toDouble();
  if (value is String) return double.tryParse(value.trim());
  return null;
}

/// Liste de codes (indices, stratégies) : chaînes nettoyées, en majuscules, sans doublon.
List<String> _codeList(dynamic value) {
  final out = <String>[];
  if (value is List) {
    for (final item in value) {
      if (item == null) continue;
      final code = _toStr(item).trim().toUpperCase();
      if (code.isNotEmpty && !out.contains(code)) out.add(code);
    }
  }
  return List.unmodifiable(out);
}

bool _toBool(dynamic value) {
  if (value is bool) return value;
  if (value is num) return value != 0;
  if (value is String) {
    final text = value.trim().toLowerCase();
    return text == 'true' || text == '1';
  }
  return false;
}

final RegExp _tzSuffix = RegExp(r'(z|[+-]\d{2}(:?\d{2})?)$', caseSensitive: false);

/// ISO 8601 ; une date-heure sans fuseau est interprétée comme UTC (stockage du backend).
DateTime? _toDate(dynamic value) {
  if (value is num) {
    // Epoch en secondes.
    return DateTime.fromMillisecondsSinceEpoch((value * 1000).round(), isUtc: true);
  }
  if (value is! String) return null;
  final text = value.trim();
  if (text.isEmpty) return null;
  final timeStart = text.indexOf(RegExp('[Tt ]'));
  final hasZone = timeStart < 0 || _tzSuffix.hasMatch(text.substring(timeStart + 1));
  return DateTime.tryParse(hasZone ? text : '${text}Z') ?? DateTime.tryParse(text);
}

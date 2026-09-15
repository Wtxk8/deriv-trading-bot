import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// Client HTTP vers l'API de copy trading (suivi d'un trader maître).
///
/// Le token API Deriv transmis à [follow] n'est ni journalisé ni conservé
/// côté app : il part uniquement dans le corps de la requête HTTPS.
class CopyService {
  CopyService({this.baseUrl = 'https://api1.innovahub226.com'});

  final String baseUrl;

  Map<String, String> _headers(String jwt) => {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': 'Bearer $jwt',
      };

  Uri _uri(String path, [Map<String, String>? query]) =>
      Uri.parse('$baseUrl$path').replace(queryParameters: query);

  /// État copy trading de l'utilisateur courant (GET /copy/me).
  Future<CopyMe> fetchMe(String jwt) async {
    final body = await _send(() => http.get(_uri('/copy/me'), headers: _headers(jwt)));
    return CopyMe.fromJson(_asMap(body));
  }

  /// Traders maîtres ouverts à la copie, avec leurs statistiques sur 30 jours.
  Future<List<MasterInfo>> fetchMasters(String jwt) async {
    final body = await _send(() => http.get(_uri('/copy/masters'), headers: _headers(jwt)));
    return _asList(body, 'masters').map(MasterInfo.fromJson).toList(growable: false);
  }

  /// Commence à copier [masterId] sur le compte Deriv associé à [apiToken].
  Future<FollowOut> follow(
    String jwt, {
    required int masterId,
    required String apiToken,
    required String accountType,
    double multiplier = 1.0,
    double maxStake = 10,
    double dailyStopLoss = 20,
    required bool consent,
  }) async {
    final body = await _send(
      () => http.post(
        _uri('/copy/follow'),
        headers: _headers(jwt),
        body: jsonEncode(<String, dynamic>{
          'master_id': masterId,
          'api_token': apiToken,
          'account_type': accountType,
          'multiplier': multiplier,
          'max_stake': maxStake,
          'daily_stop_loss': dailyStopLoss,
          'consent': consent,
        }),
      ),
      // Le serveur valide le token auprès de Deriv avant de répondre.
      timeout: const Duration(seconds: 30),
    );
    return FollowOut.fromJson(_asMap(body));
  }

  /// Modifie les réglages du suivi en cours (seuls les champs fournis sont envoyés).
  Future<FollowOut> updateFollow(
    String jwt, {
    double? multiplier,
    double? maxStake,
    double? dailyStopLoss,
    bool? active,
  }) async {
    final body = await _send(
      () => http.patch(
        _uri('/copy/follow'),
        headers: _headers(jwt),
        body: jsonEncode(<String, dynamic>{
          if (multiplier != null) 'multiplier': multiplier,
          if (maxStake != null) 'max_stake': maxStake,
          if (dailyStopLoss != null) 'daily_stop_loss': dailyStopLoss,
          if (active != null) 'active': active,
        }),
      ),
    );
    return FollowOut.fromJson(_asMap(body));
  }

  /// Arrête la copie (le serveur supprime aussi le token chiffré). 204 attendu.
  Future<void> unfollow(String jwt) async {
    await _send(() => http.delete(_uri('/copy/follow'), headers: _headers(jwt)));
  }

  /// Historique des trades copiés sur le compte de l'utilisateur.
  Future<List<CopiedTrade>> fetchTrades(String jwt, {int limit = 50}) async {
    final body = await _send(
      () => http.get(_uri('/copy/trades', {'limit': '$limit'}), headers: _headers(jwt)),
    );
    return _asList(body, 'trades').map(CopiedTrade.fromJson).toList(growable: false);
  }

  /// Exécute la requête et convertit erreurs réseau / délais en [CopyServiceException].
  Future<dynamic> _send(
    Future<http.Response> Function() request, {
    Duration timeout = const Duration(seconds: 20),
  }) async {
    final http.Response response;
    try {
      response = await request().timeout(timeout);
    } on TimeoutException {
      throw CopyServiceException(0, 'Délai dépassé : le serveur ne répond pas.');
    } catch (_) {
      // ClientException, SocketException, erreur TLS… : message neutre, sans détail technique.
      throw CopyServiceException(0, 'Serveur injoignable : vérifiez votre connexion.');
    }
    return _decode(response);
  }

  dynamic _decode(http.Response response) {
    // FastAPI n'indique pas de charset : on décode explicitement en UTF-8 (accents).
    final String text = utf8.decode(response.bodyBytes, allowMalformed: true);
    dynamic body;
    if (text.isNotEmpty) {
      try {
        body = jsonDecode(text);
      } on FormatException {
        body = null; // Page HTML d'un proxy (Cloudflare…) : on n'expose pas son contenu.
      }
    }
    if (response.statusCode >= 400) {
      throw CopyServiceException(response.statusCode, _detailOf(body));
    }
    if (body == null && text.isNotEmpty) {
      throw CopyServiceException(response.statusCode, 'Réponse inattendue du serveur.');
    }
    return body;
  }

  /// Extrait un message lisible de `detail` (chaîne, ou liste d'erreurs de validation).
  /// Seuls les champs `msg` sont repris : l'`input` renvoyé en 422 peut contenir le token.
  static String _detailOf(dynamic body) {
    if (body is! Map) return '';
    final dynamic detail = body['detail'] ?? body['message'];
    if (detail is String) return detail;
    if (detail is List) {
      return detail.whereType<Map>().map((e) => e['msg']).whereType<String>().join(' ; ');
    }
    if (detail is Map) {
      final dynamic msg = detail['msg'] ?? detail['message'];
      return msg is String ? msg : '';
    }
    return '';
  }
}

/// État copy trading de l'utilisateur (GET /copy/me).
class CopyMe {
  const CopyMe({
    required this.enabled,
    required this.isMaster,
    required this.following,
    required this.followersCount,
  });

  /// Fonction active côté serveur.
  final bool enabled;
  final bool isMaster;

  /// Suivi en cours, null si l'utilisateur ne copie personne.
  final FollowOut? following;

  /// Nombre d'abonnés (utile si [isMaster]).
  final int followersCount;

  CopyMe withFollowing(FollowOut? value) => CopyMe(
        enabled: enabled,
        isMaster: isMaster,
        following: value,
        followersCount: followersCount,
      );

  factory CopyMe.fromJson(Map<String, dynamic> json) {
    final dynamic f = json['following'];
    return CopyMe(
      enabled: _toBool(json['enabled']) ?? false,
      isMaster: _toBool(json['is_master']) ?? false,
      following: f is Map ? FollowOut.fromJson(_asMap(f)) : null,
      followersCount: _toInt(json['followers_count']) ?? 0,
    );
  }
}

/// Suivi d'un trader maître (FollowOut du contrat d'API).
class FollowOut {
  const FollowOut({
    required this.masterId,
    required this.masterName,
    required this.accountType,
    required this.accountCurrency,
    required this.multiplier,
    required this.maxStake,
    required this.dailyStopLoss,
    required this.active,
    required this.todayPnl,
    required this.pausedReason,
    required this.createdAt,
  });

  final int masterId;
  final String masterName;
  final String accountType; // demo | real
  final String accountCurrency;
  final double multiplier;
  final double maxStake;
  final double dailyStopLoss;
  final bool active;
  final double todayPnl;

  /// Motif de pause posé par le serveur (ex. « daily_stop_loss »), null sinon.
  final String? pausedReason;
  final DateTime? createdAt;

  bool get isReal => accountType == 'real';

  /// Copie suspendue : désactivée par l'utilisateur ou mise en pause par le serveur.
  bool get isPaused => !active || pausedReason != null;

  factory FollowOut.fromJson(Map<String, dynamic> json) {
    final String reason = (_toStr(json['paused_reason']) ?? '').trim();
    final int masterId = _toInt(json['master_id']) ?? 0;
    final String name = (_toStr(json['master_name']) ?? '').trim();
    return FollowOut(
      masterId: masterId,
      masterName: name.isEmpty ? 'Trader #$masterId' : name,
      accountType: (_toStr(json['account_type']) ?? 'demo').toLowerCase(),
      accountCurrency: _toStr(json['account_currency']) ?? '',
      multiplier: _toDouble(json['multiplier']) ?? 1.0,
      maxStake: _toDouble(json['max_stake']) ?? 10,
      dailyStopLoss: _toDouble(json['daily_stop_loss']) ?? 20,
      active: _toBool(json['active']) ?? true,
      todayPnl: _toDouble(json['today_pnl']) ?? 0,
      pausedReason: reason.isEmpty ? null : reason,
      createdAt: _toDate(json['created_at']),
    );
  }
}

/// Statistiques d'un trader maître sur la fenêtre glissante.
class MasterStats {
  const MasterStats({this.trades, this.winRate, this.pnl, this.windowDays = 30});

  final int? trades;

  /// Fraction 0..1 (comme /signals/stats), null sans trade gagné ni perdu.
  final double? winRate;
  final double? pnl;
  final int windowDays;

  /// Taux de réussite en pourcentage ; tolère un serveur renvoyant déjà un pourcentage.
  double? get winRatePercent {
    final r = winRate;
    if (r == null) return null;
    return r <= 1 ? r * 100 : r;
  }

  factory MasterStats.fromJson(Map<String, dynamic> json) => MasterStats(
        trades: _toInt(json['trades']),
        winRate: _toDouble(json['win_rate']),
        pnl: _toDouble(json['pnl']),
        windowDays: _toInt(json['window_days']) ?? 30,
      );
}

/// Trader maître proposé à la copie (GET /copy/masters).
class MasterInfo {
  const MasterInfo({
    required this.masterId,
    required this.displayName,
    required this.bio,
    required this.followers,
    required this.stats,
  });

  final int masterId;
  final String displayName;
  final String bio;
  final int followers;
  final MasterStats stats;

  factory MasterInfo.fromJson(Map<String, dynamic> json) {
    final int id = _toInt(json['master_id']) ?? 0;
    final String name = (_toStr(json['display_name']) ?? '').trim();
    return MasterInfo(
      masterId: id,
      displayName: name.isEmpty ? 'Trader #$id' : name,
      bio: (_toStr(json['bio']) ?? '').trim(),
      followers: _toInt(json['followers']) ?? 0,
      stats: MasterStats.fromJson(_asMap(json['stats'])),
    );
  }
}

/// Trade répliqué sur le compte du suiveur (GET /copy/trades).
class CopiedTrade {
  const CopiedTrade({
    required this.id,
    required this.masterContractId,
    required this.followerContractId,
    required this.symbol,
    required this.contractType,
    required this.stake,
    required this.profit,
    required this.status,
    required this.reason,
    required this.createdAt,
  });

  final int id;
  final int? masterContractId;
  final int? followerContractId;
  final String symbol;
  final String contractType;
  final double stake;
  final double? profit;

  /// open | won | lost | failed | skipped
  final String status;
  final String? reason;
  final DateTime? createdAt;

  factory CopiedTrade.fromJson(Map<String, dynamic> json) {
    final String reason = (_toStr(json['reason']) ?? '').trim();
    return CopiedTrade(
      id: _toInt(json['id']) ?? 0,
      masterContractId: _toInt(json['master_contract_id']),
      followerContractId: _toInt(json['follower_contract_id']),
      symbol: _toStr(json['symbol']) ?? '',
      contractType: _toStr(json['contract_type']) ?? '',
      stake: _toDouble(json['stake']) ?? 0,
      profit: _toDouble(json['profit']),
      status: (_toStr(json['status']) ?? '').toLowerCase(),
      reason: reason.isEmpty ? null : reason,
      createdAt: _toDate(json['created_at']),
    );
  }
}

/// Erreur d'appel à l'API copy trading (statusCode 0 = réseau ou délai dépassé).
class CopyServiceException implements Exception {
  CopyServiceException(this.statusCode, this.detail);

  final int statusCode;
  final String detail;

  /// Message prêt à afficher selon le code HTTP du contrat d'API.
  /// [notFound] remplace le message 404 par défaut (trader maître introuvable).
  String userMessage({String? notFound}) {
    switch (statusCode) {
      case 0:
        return detail.isNotEmpty ? detail : 'Serveur injoignable : vérifiez votre connexion.';
      case 400:
        return detail.isNotEmpty
            ? detail
            : 'Requête refusée : token Deriv invalide, compte du type demandé introuvable, '
                'ou tentative de vous copier vous-même.';
      case 401:
        return 'Session expirée : reconnectez-vous.';
      case 402:
        return 'Ni essai gratuit ni Premium actif : passez au Premium pour copier un trader.';
      case 403:
        return 'Accès refusé.';
      case 404:
        return notFound ?? 'Ce trader maître est introuvable ou a été désactivé.';
      case 409:
        return 'Vous copiez déjà un trader : arrêtez la copie en cours avant d\'en suivre un autre.';
      case 422:
        return 'Réglages refusés par le serveur : vérifiez les valeurs saisies et le consentement.';
      case 429:
        return 'Trop de requêtes : réessayez dans un instant.';
      case 503:
        return 'Le copy trading est momentanément désactivé sur le serveur.';
      default:
        return statusCode >= 500 ? 'Erreur serveur : réessayez plus tard.' : toString();
    }
  }

  @override
  String toString() => 'Erreur $statusCode : $detail';
}

// --- Lecture tolérante du JSON (types approximatifs, champs absents) ---

Map<String, dynamic> _asMap(dynamic v) {
  if (v is Map<String, dynamic>) return v;
  if (v is Map) return v.map((k, val) => MapEntry(k.toString(), val));
  return const <String, dynamic>{};
}

List<Map<String, dynamic>> _asList(dynamic body, String key) {
  final dynamic raw = body is List ? body : (body is Map ? body[key] : null);
  if (raw is! List) return const [];
  return raw.whereType<Map>().map(_asMap).toList(growable: false);
}

String? _toStr(dynamic v) {
  if (v == null) return null;
  return v is String ? v : v.toString();
}

double? _toDouble(dynamic v) {
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v.trim().replaceAll(',', '.'));
  return null;
}

int? _toInt(dynamic v) {
  if (v is int) return v;
  if (v is num) return v.toInt();
  if (v is String) {
    final s = v.trim();
    return int.tryParse(s) ?? double.tryParse(s)?.toInt();
  }
  return null;
}

bool? _toBool(dynamic v) {
  if (v is bool) return v;
  if (v is num) return v != 0;
  if (v is String) {
    final s = v.trim().toLowerCase();
    if (s == 'true' || s == '1') return true;
    if (s == 'false' || s == '0') return false;
  }
  return null;
}

final RegExp _tzSuffix = RegExp(r'(Z|[+-]\d{2}(:?\d{2})?)$');

DateTime? _toDate(dynamic v) {
  if (v is String && v.isNotEmpty) {
    // SQLite restitue les dates sans fuseau alors que le backend les écrit en UTC.
    final bool hasTime = v.contains('T') || v.contains(' ');
    final String iso = hasTime && !_tzSuffix.hasMatch(v) ? '${v}Z' : v;
    return DateTime.tryParse(iso);
  }
  if (v is num) {
    return DateTime.fromMillisecondsSinceEpoch((v * 1000).round(), isUtc: true);
  }
  return null;
}

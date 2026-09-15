import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../theme/app_theme.dart';
import 'signal_service.dart';

/// Notifications locales des nouveaux signaux (canal Android « signals »).
///
/// Initialisation paresseuse : rien n'est fait tant qu'aucune notification
/// n'est affichée ni aucune permission demandée. Les erreurs du plugin
/// (plateforme non supportée, tests) sont absorbées : une notification
/// manquée ne doit jamais casser l'écran.
class NotificationService {
  NotificationService({FlutterLocalNotificationsPlugin? plugin})
      : _plugin = plugin ?? FlutterLocalNotificationsPlugin();

  /// Instance partagée par l'app.
  static final NotificationService instance = NotificationService();

  static const String channelId = 'signals';
  static const String channelName = 'Signaux de trading';
  static const String channelDescription = 'Alerte à chaque nouveau signal publié.';

  final FlutterLocalNotificationsPlugin _plugin;
  Future<bool>? _ready;
  Future<bool>? _permission;

  AndroidFlutterLocalNotificationsPlugin? get _android =>
      _plugin.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>();

  Future<bool> _ensureInitialized() => _ready ??= _initialize();

  Future<bool> _initialize() async {
    try {
      const settings = InitializationSettings(
        android: AndroidInitializationSettings('@mipmap/ic_launcher'),
        // Aucune demande implicite à l'initialisation : elle passe par requestPermission().
        iOS: DarwinInitializationSettings(
          requestAlertPermission: false,
          requestBadgePermission: false,
          requestSoundPermission: false,
        ),
      );
      await _plugin.initialize(settings: settings);
      await _android?.createNotificationChannel(
        const AndroidNotificationChannel(
          channelId,
          channelName,
          description: channelDescription,
          importance: Importance.high,
        ),
      );
      return true;
    } catch (e) {
      debugPrint('Notifications indisponibles : $e');
      return false;
    }
  }

  /// Demande l'autorisation d'afficher des notifications (Android 13+ ; iOS).
  ///
  /// Une seule demande par session de l'app. Renvoie true si elles sont autorisées.
  Future<bool> requestPermission() => _permission ??= _requestPermission();

  Future<bool> _requestPermission() async {
    if (!await _ensureInitialized()) return false;
    try {
      final android = _android;
      if (android != null) {
        // Avant Android 13 : pas de boîte de dialogue, renvoie l'état courant.
        return await android.requestNotificationsPermission() ?? false;
      }
      final ios = _plugin.resolvePlatformSpecificImplementation<IOSFlutterLocalNotificationsPlugin>();
      if (ios != null) {
        return await ios.requestPermissions(alert: true, sound: true) ?? false;
      }
      return false;
    } catch (e) {
      debugPrint('Permission de notification non obtenue : $e');
      return false;
    }
  }

  /// Affiche la notification d'un nouveau signal :
  /// « ACHAT · Volatility 75 Index » / « Entrée X · SL Y · TP Z ».
  Future<void> showSignal(Signal signal) async {
    if (!await _ensureInitialized()) return;
    final title = '${signal.isBuy ? 'ACHAT' : 'VENTE'} · ${signal.displayName}';
    final body = 'Entrée ${formatSignalPrice(signal.entry)} · '
        'SL ${formatSignalPrice(signal.stopLoss)} · '
        'TP ${formatSignalPrice(signal.takeProfit)}';
    try {
      await _plugin.show(
        id: signal.id & 0x7fffffff, // identifiant de notification Android sur 32 bits
        title: title,
        body: body,
        notificationDetails: NotificationDetails(
          android: AndroidNotificationDetails(
            channelId,
            channelName,
            channelDescription: channelDescription,
            importance: Importance.high,
            priority: Priority.high,
            color: signal.isBuy ? AppColors.success : AppColors.danger,
            ticker: title,
            styleInformation: BigTextStyleInformation(body),
          ),
          iOS: const DarwinNotificationDetails(),
        ),
        payload: 'signal:${signal.id}',
      );
    } catch (e) {
      debugPrint('Notification non affichée : $e');
    }
  }
}

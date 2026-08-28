import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../services/billing_service.dart';
import 'auth_provider.dart';

final billingServiceProvider = Provider<BillingService>((ref) => BillingService());

/// Statut d'abonnement (essai / premium) — auto-refresh à chaque changement de JWT.
final subscriptionStatusProvider = FutureProvider.autoDispose<SubscriptionStatus?>((ref) async {
  final jwt = ref.watch(jwtProvider);
  if (jwt == null || jwt.isEmpty) return null;
  return ref.watch(billingServiceProvider).fetchStatus(jwt);
});

/// Plans disponibles (public, pas besoin de JWT).
final billingPlansProvider = FutureProvider.autoDispose<Map<String, PlanInfo>>((ref) async {
  return ref.watch(billingServiceProvider).fetchPlans();
});

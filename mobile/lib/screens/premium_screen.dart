import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:url_launcher/url_launcher.dart';

import '../providers/auth_provider.dart';
import '../providers/billing_provider.dart';
import '../services/billing_service.dart';
import '../theme/app_theme.dart';
import 'login_screen.dart';

/// Écran d'abonnement Premium : affiche l'état de l'essai + les plans + checkout Mobile Money.
class PremiumScreen extends ConsumerStatefulWidget {
  const PremiumScreen({super.key});

  @override
  ConsumerState<PremiumScreen> createState() => _PremiumScreenState();
}

class _PremiumScreenState extends ConsumerState<PremiumScreen> {
  String _selectedPlan = 'premium_monthly';
  final TextEditingController _phoneCtrl = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    _phoneCtrl.dispose();
    super.dispose();
  }

  Future<void> _openLogin() async {
    await Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => const LoginScreen()));
    if (!mounted) return;
    ref.invalidate(subscriptionStatusProvider);
  }

  Future<void> _checkout() async {
    final jwt = ref.read(jwtProvider);
    if (jwt == null || jwt.isEmpty) {
      await _openLogin();
      return;
    }
    setState(() => _busy = true);
    try {
      final session = await ref.read(billingServiceProvider).initCheckout(
            jwt: jwt,
            plan: _selectedPlan,
            provider: 'fedapay',
            phone: _phoneCtrl.text.trim().isEmpty ? null : _phoneCtrl.text.trim(),
          );
      if (session.checkoutUrl.isEmpty) {
        _snack('URL de checkout indisponible');
        return;
      }
      final ok = await launchUrl(Uri.parse(session.checkoutUrl), mode: LaunchMode.externalApplication);
      if (!ok) _snack('Impossible d\'ouvrir la page de paiement');
      // On rafraîchit le statut au retour (le webhook activera l'abonnement).
      if (mounted) ref.invalidate(subscriptionStatusProvider);
    } on BillingServiceException catch (e) {
      _snack(e.toString());
    } catch (e) {
      _snack('Échec : $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _snack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  Widget build(BuildContext context) {
    final statusAsync = ref.watch(subscriptionStatusProvider);
    final plansAsync = ref.watch(billingPlansProvider);
    final isLoggedIn = ref.watch(jwtProvider) != null;

    return Scaffold(
      appBar: AppBar(
        title: Text('Abonnement Premium', style: AppTheme.heading(fontSize: 15, letterSpacing: -0.2)),
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
          children: [
            _StatusCard(status: statusAsync, isLoggedIn: isLoggedIn, onLogin: _openLogin),
            const SizedBox(height: 18),
            Text('Choisissez votre formule', style: AppTheme.heading(fontSize: 14, letterSpacing: 0.2)),
            const SizedBox(height: 12),
            plansAsync.when(
              data: (plans) => Column(
                children: [
                  for (final p in plans.values) ...[
                    _PlanCard(
                      plan: p,
                      selected: _selectedPlan == p.key,
                      onTap: () => setState(() => _selectedPlan = p.key),
                    ),
                    const SizedBox(height: 10),
                  ],
                ],
              ),
              loading: () => const Padding(
                padding: EdgeInsets.symmetric(vertical: 24),
                child: Center(child: CircularProgressIndicator(color: AppColors.primary)),
              ),
              error: (e, _) => Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text('Impossible de charger les formules : $e',
                    style: GoogleFonts.manrope(color: AppColors.danger, fontSize: 12.5)),
              ),
            ),
            const SizedBox(height: 16),
            Text('Numéro Mobile Money (optionnel)', style: AppTheme.labelMicro().copyWith(fontSize: 11.5)),
            const SizedBox(height: 8),
            TextField(
              controller: _phoneCtrl,
              keyboardType: TextInputType.phone,
              style: AppTheme.mono(fontSize: 14, color: AppColors.textPrimary),
              decoration: const InputDecoration(hintText: '+225 07 00 00 00 00'),
            ),
            const SizedBox(height: 18),
            SizedBox(
              height: 56,
              child: FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: AppColors.primary,
                  foregroundColor: Colors.white,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadii.lg - 2)),
                ),
                onPressed: _busy ? null : _checkout,
                child: _busy
                    ? const SizedBox(
                        width: 22,
                        height: 22,
                        child: CircularProgressIndicator(strokeWidth: 2, valueColor: AlwaysStoppedAnimation(Colors.white)),
                      )
                    : Text(isLoggedIn ? 'Payer par Mobile Money' : 'Se connecter pour continuer',
                        style: GoogleFonts.manrope(fontSize: 15, fontWeight: FontWeight.w800)),
              ),
            ),
            const SizedBox(height: 14),
            Text(
              'Paiement sécurisé via FedaPay. Votre abonnement est activé automatiquement dès réception du paiement.',
              style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary, height: 1.4),
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.status, required this.isLoggedIn, required this.onLogin});
  final AsyncValue<SubscriptionStatus?> status;
  final bool isLoggedIn;
  final VoidCallback onLogin;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: AppTheme.cardGradient(),
      child: status.when(
        data: (s) {
          if (!isLoggedIn || s == null) {
            return _line('Non connecté', 'Connectez-vous pour voir votre essai et vos abonnements.', AppColors.warning,
                cta: TextButton(onPressed: onLogin, child: const Text('Se connecter')));
          }
          if (s.premiumActive) {
            final until = s.premiumExpiresAt == null
                ? 'illimité'
                : 'jusqu\'au ${_fmtDate(s.premiumExpiresAt!)}';
            return _line('Premium actif', until, AppColors.success);
          }
          if (s.trialActive) {
            return _line(
              'Essai gratuit — ${s.trialDaysRemaining} jour${s.trialDaysRemaining > 1 ? "s" : ""} restant',
              s.trialExpiresAt != null ? 'Se termine le ${_fmtDate(s.trialExpiresAt!)}' : '',
              AppColors.primarySoft,
            );
          }
          return _line(
            'Essai terminé',
            'Passez au premium pour continuer à trader en compte réel.',
            AppColors.danger,
          );
        },
        loading: () => const Row(children: [
          SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.primary)),
          SizedBox(width: 12),
          Text('Chargement…'),
        ]),
        error: (e, _) => Text('Erreur : $e', style: GoogleFonts.manrope(color: AppColors.danger, fontSize: 12.5)),
      ),
    );
  }

  Widget _line(String title, String subtitle, Color color, {Widget? cta}) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Container(
              width: 10,
              height: 10,
              decoration: BoxDecoration(shape: BoxShape.circle, color: color),
            ),
            const SizedBox(width: 10),
            Text(title, style: AppTheme.heading(fontSize: 15)),
          ],
        ),
        if (subtitle.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(subtitle, style: GoogleFonts.manrope(fontSize: 13, color: AppColors.textSecondary, height: 1.4)),
        ],
        if (cta != null) ...[const SizedBox(height: 4), cta],
      ],
    );
  }

  String _fmtDate(DateTime dt) {
    final local = dt.toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(local.day)}/${two(local.month)}/${local.year}';
  }
}

class _PlanCard extends StatelessWidget {
  const _PlanCard({required this.plan, required this.selected, required this.onTap});
  final PlanInfo plan;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(AppRadii.lg),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: selected ? AppColors.primary.withValues(alpha: 0.14) : AppColors.surface,
          borderRadius: BorderRadius.circular(AppRadii.lg),
          border: Border.all(
            color: selected ? AppColors.primary.withValues(alpha: 0.55) : AppColors.border,
            width: 1,
          ),
        ),
        child: Row(
          children: [
            Container(
              width: 20,
              height: 20,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(
                  color: selected ? AppColors.primary : Colors.white.withValues(alpha: 0.2),
                  width: 2,
                ),
              ),
              alignment: Alignment.center,
              child: Container(
                width: 10,
                height: 10,
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
                  Text(plan.label, style: AppTheme.heading(fontSize: 14, letterSpacing: -0.2)),
                  const SizedBox(height: 4),
                  Text('${plan.durationDays} jours d\'accès Premium',
                      style: GoogleFonts.manrope(fontSize: 11.5, color: AppColors.textTertiary)),
                ],
              ),
            ),
            Column(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text('${plan.amountXof} XOF',
                    style: AppTheme.mono(fontSize: 16, fontWeight: FontWeight.w800, color: AppColors.primarySoft)),
                if (plan.durationDays >= 365)
                  Container(
                    margin: const EdgeInsets.only(top: 4),
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                    decoration: BoxDecoration(
                      color: AppColors.success.withValues(alpha: 0.14),
                      borderRadius: BorderRadius.circular(999),
                    ),
                    child: Text('-24%',
                        style: AppTheme.mono(fontSize: 10, fontWeight: FontWeight.w800, color: AppColors.success)),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

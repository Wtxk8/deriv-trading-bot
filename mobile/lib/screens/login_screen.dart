import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../providers/auth_provider.dart';
import '../services/auth_service.dart';
import '../theme/app_theme.dart';
import '../widgets/brand_logo.dart';
import 'dashboard_screen.dart';

/// Connexion au compte applicatif (email + mdp) — dark premium.
class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final TextEditingController _email = TextEditingController();
  final TextEditingController _password = TextEditingController();
  bool _obscure = true;
  bool _busy = false;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _login() async {
    final email = _email.text.trim();
    final password = _password.text;
    if (email.isEmpty || password.isEmpty) {
      _snack('Email et mot de passe requis');
      return;
    }
    setState(() => _busy = true);
    try {
      final jwt = await ref.read(authServiceProvider).login(email, password);
      await ref.read(jwtProvider.notifier).save(jwt);
      if (!mounted) return;
      Navigator.of(context).pushReplacement(
        MaterialPageRoute<void>(builder: (_) => const DashboardScreen()),
      );
    } on AuthServiceException catch (e) {
      _snack(e.toString());
    } catch (e) {
      _snack('Échec de connexion : $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _snack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(icon: const Icon(Icons.arrow_back, size: 20), onPressed: () => Navigator.of(context).pop()),
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 20, 24, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const BrandLogo(size: 46, letter: 'D'),
              const SizedBox(height: 30),
              Text('Bienvenue sur\nDeriv Trading Bot',
                  style: AppTheme.heading(fontSize: 30, letterSpacing: -0.8).copyWith(height: 1.12)),
              const SizedBox(height: 12),
              Text(
                'Pilotage et supervision de votre robot de trading connecté à Deriv.',
                style: GoogleFonts.manrope(fontSize: 14.5, height: 1.5, color: AppColors.textTertiary),
              ),
              const SizedBox(height: 38),
              Padding(
                padding: const EdgeInsets.only(bottom: 8, left: 4),
                child: Text('ADRESSE E-MAIL', style: AppTheme.labelMicro()),
              ),
              TextField(
                controller: _email,
                keyboardType: TextInputType.emailAddress,
                autocorrect: false,
                enableSuggestions: false,
                style: GoogleFonts.manrope(color: AppColors.textPrimary, fontSize: 15.5, fontWeight: FontWeight.w500),
                decoration: const InputDecoration(hintText: 'vous@exemple.com'),
              ),
              const SizedBox(height: 14),
              Padding(
                padding: const EdgeInsets.only(bottom: 8, left: 4),
                child: Text('MOT DE PASSE', style: AppTheme.labelMicro()),
              ),
              TextField(
                controller: _password,
                obscureText: _obscure,
                autocorrect: false,
                enableSuggestions: false,
                onSubmitted: (_) => _busy ? null : _login(),
                style: GoogleFonts.manrope(color: AppColors.textPrimary, fontSize: 15.5, fontWeight: FontWeight.w500),
                decoration: InputDecoration(
                  hintText: '••••••••••',
                  suffixIcon: TextButton(
                    onPressed: () => setState(() => _obscure = !_obscure),
                    child: Text(_obscure ? 'Voir' : 'Masquer',
                        style: GoogleFonts.manrope(color: AppColors.textTertiary, fontSize: 11, fontWeight: FontWeight.w700)),
                  ),
                ),
              ),
              const SizedBox(height: 26),
              _PrimaryButton(label: 'Se connecter', busy: _busy, onPressed: _busy ? null : _login),
              const SizedBox(height: 18),
              Center(
                child: Text.rich(
                  TextSpan(
                    text: 'Mot de passe oublié ? ',
                    style: GoogleFonts.manrope(fontSize: 13.5, color: AppColors.textTertiary),
                    children: [
                      TextSpan(
                        text: 'Réinitialiser',
                        style: GoogleFonts.manrope(fontSize: 13.5, color: AppColors.primarySoft, fontWeight: FontWeight.w700),
                      ),
                    ],
                  ),
                ),
              ),
              const Spacer(),
              Container(
                padding: const EdgeInsets.all(14),
                decoration: AppTheme.card(radius: AppRadii.md, color: AppColors.surfaceLow, border: AppColors.borderSoft),
                child: Row(
                  children: [
                    Container(
                      width: 30,
                      height: 30,
                      decoration: BoxDecoration(
                        color: AppColors.success.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(9),
                      ),
                      alignment: Alignment.center,
                      child: const Icon(Icons.check_rounded, color: AppColors.success, size: 16),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        'Chiffrement local du token API. Aucune clé n\'est transmise à nos serveurs.',
                        style: GoogleFonts.manrope(fontSize: 11.5, height: 1.45, color: AppColors.textTertiary),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _PrimaryButton extends StatelessWidget {
  const _PrimaryButton({required this.label, required this.onPressed, this.busy = false});
  final String label;
  final VoidCallback? onPressed;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 56,
      child: DecoratedBox(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(AppRadii.lg - 2),
          gradient: const LinearGradient(
            begin: Alignment(-0.5, -1),
            end: Alignment(1, 1),
            colors: [AppColors.primary, AppColors.primaryDeep],
          ),
          boxShadow: [
            BoxShadow(color: AppColors.primary.withValues(alpha: 0.55), blurRadius: 30, offset: const Offset(0, 14), spreadRadius: -12),
          ],
        ),
        child: Material(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(AppRadii.lg - 2),
          child: InkWell(
            borderRadius: BorderRadius.circular(AppRadii.lg - 2),
            onTap: onPressed,
            child: Center(
              child: busy
                  ? const SizedBox(
                      width: 22,
                      height: 22,
                      child: CircularProgressIndicator(strokeWidth: 2, valueColor: AlwaysStoppedAnimation(Colors.white)),
                    )
                  : Text(label, style: GoogleFonts.manrope(fontSize: 16, fontWeight: FontWeight.w800, color: Colors.white, letterSpacing: 0.2)),
            ),
          ),
        ),
      ),
    );
  }
}

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../providers/auth_provider.dart';
import '../services/auth_service.dart';
import '../theme/app_theme.dart';
import '../widgets/brand_logo.dart';
import 'dashboard_screen.dart';

/// Création de compte — déclenche l'essai gratuit 7 jours + auto-login.
class RegisterScreen extends ConsumerStatefulWidget {
  const RegisterScreen({super.key});

  @override
  ConsumerState<RegisterScreen> createState() => _RegisterScreenState();
}

class _RegisterScreenState extends ConsumerState<RegisterScreen> {
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _confirm = TextEditingController();
  bool _obscure = true;
  bool _busy = false;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _confirm.dispose();
    super.dispose();
  }

  Future<void> _register() async {
    final email = _email.text.trim();
    final pwd = _password.text;
    final confirm = _confirm.text;
    if (email.isEmpty || pwd.isEmpty) {
      _snack('Email et mot de passe requis');
      return;
    }
    if (pwd.length < 8) {
      _snack('Le mot de passe doit contenir au moins 8 caractères');
      return;
    }
    if (pwd != confirm) {
      _snack('Les mots de passe ne correspondent pas');
      return;
    }
    setState(() => _busy = true);
    try {
      final jwt = await ref.read(authServiceProvider).register(email: email, password: pwd);
      await ref.read(jwtProvider.notifier).save(jwt);
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(
        MaterialPageRoute<void>(builder: (_) => const DashboardScreen()),
        (route) => false,
      );
    } on AuthServiceException catch (e) {
      _snack(e.toString());
    } catch (e) {
      _snack('Échec : $e');
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
              const SizedBox(height: 24),
              Text('Créer votre compte', style: AppTheme.heading(fontSize: 28, letterSpacing: -0.8).copyWith(height: 1.12)),
              const SizedBox(height: 12),
              Text(
                '7 jours d\'essai gratuit sur compte réel Deriv, puis abonnement Premium par Mobile Money.',
                style: GoogleFonts.manrope(fontSize: 14, height: 1.5, color: AppColors.textTertiary),
              ),
              const SizedBox(height: 28),
              _label('ADRESSE E-MAIL'),
              TextField(
                controller: _email,
                keyboardType: TextInputType.emailAddress,
                autocorrect: false,
                enableSuggestions: false,
                style: GoogleFonts.manrope(color: AppColors.textPrimary, fontSize: 15.5, fontWeight: FontWeight.w500),
                decoration: const InputDecoration(hintText: 'vous@exemple.com'),
              ),
              const SizedBox(height: 14),
              _label('MOT DE PASSE'),
              TextField(
                controller: _password,
                obscureText: _obscure,
                autocorrect: false,
                enableSuggestions: false,
                style: GoogleFonts.manrope(color: AppColors.textPrimary, fontSize: 15.5, fontWeight: FontWeight.w500),
                decoration: InputDecoration(
                  hintText: 'Minimum 8 caractères',
                  suffixIcon: TextButton(
                    onPressed: () => setState(() => _obscure = !_obscure),
                    child: Text(_obscure ? 'Voir' : 'Masquer',
                        style: GoogleFonts.manrope(color: AppColors.textTertiary, fontSize: 11, fontWeight: FontWeight.w700)),
                  ),
                ),
              ),
              const SizedBox(height: 14),
              _label('CONFIRMER LE MOT DE PASSE'),
              TextField(
                controller: _confirm,
                obscureText: _obscure,
                autocorrect: false,
                enableSuggestions: false,
                onSubmitted: (_) => _busy ? null : _register(),
                style: GoogleFonts.manrope(color: AppColors.textPrimary, fontSize: 15.5, fontWeight: FontWeight.w500),
                decoration: const InputDecoration(hintText: '••••••••••'),
              ),
              const SizedBox(height: 24),
              _PrimaryButton(label: 'Créer mon compte', busy: _busy, onPressed: _busy ? null : _register),
              const SizedBox(height: 18),
              Center(
                child: TextButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: Text.rich(
                    TextSpan(
                      text: 'Déjà inscrit ? ',
                      style: GoogleFonts.manrope(fontSize: 13.5, color: AppColors.textTertiary),
                      children: [
                        TextSpan(
                          text: 'Se connecter',
                          style: GoogleFonts.manrope(fontSize: 13.5, color: AppColors.primarySoft, fontWeight: FontWeight.w700),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _label(String txt) => Padding(
        padding: const EdgeInsets.only(bottom: 8, left: 4),
        child: Text(txt, style: AppTheme.labelMicro()),
      );
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

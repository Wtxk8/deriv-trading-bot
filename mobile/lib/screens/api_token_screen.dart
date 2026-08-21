import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../providers/bot_provider.dart';
import '../theme/app_theme.dart';
import 'dashboard_screen.dart';

/// Saisie et stockage sécurisé du token API Deriv (design dark premium).
class ApiTokenScreen extends ConsumerStatefulWidget {
  const ApiTokenScreen({super.key});

  @override
  ConsumerState<ApiTokenScreen> createState() => _ApiTokenScreenState();
}

class _ApiTokenScreenState extends ConsumerState<ApiTokenScreen> {
  final TextEditingController _controller = TextEditingController();
  bool _obscure = true;
  bool _saving = false;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final token = _controller.text.trim();
    if (token.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Token requis')),
      );
      return;
    }
    setState(() => _saving = true);
    await ref.read(tokenProvider.notifier).save(token);
    if (!mounted) return;
    Navigator.of(context).pushReplacement(
      MaterialPageRoute<void>(builder: (_) => const DashboardScreen()),
    );
  }

  @override
  Widget build(BuildContext context) {
    final tok = _controller.text.trim();
    final valid = tok.length >= 8;

    return Scaffold(
      appBar: AppBar(
        leading: Navigator.canPop(context)
            ? IconButton(
                icon: const Icon(Icons.arrow_back, size: 20),
                onPressed: () => Navigator.of(context).pop(),
              )
            : null,
        title: Text('Connexion Deriv', style: AppTheme.heading(fontSize: 15, letterSpacing: -0.2)),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 20),
            child: Center(
              child: Text('2 / 2', style: AppTheme.mono(fontSize: 11, fontWeight: FontWeight.w700, color: AppColors.textTertiary)),
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 12, 24, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const SizedBox(height: 12),
              // Hero card
              Container(
                padding: const EdgeInsets.all(22),
                decoration: AppTheme.cardGradient(),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Container(
                          width: 40,
                          height: 40,
                          decoration: BoxDecoration(
                            color: AppColors.primary.withValues(alpha: 0.14),
                            borderRadius: BorderRadius.circular(12),
                          ),
                          alignment: Alignment.center,
                          child: const Icon(Icons.vpn_key_rounded, color: AppColors.primarySoft, size: 18),
                        ),
                        const Spacer(),
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                          decoration: BoxDecoration(
                            color: AppColors.success.withValues(alpha: 0.12),
                            borderRadius: BorderRadius.circular(999),
                          ),
                          child: Text('WSS · READY',
                              style: AppTheme.mono(fontSize: 10.5, fontWeight: FontWeight.w700, letterSpacing: 1, color: AppColors.success)),
                        ),
                      ],
                    ),
                    const SizedBox(height: 18),
                    Text('Token API Deriv', style: AppTheme.heading(fontSize: 20)),
                    const SizedBox(height: 8),
                    Text(
                      'Créez un token avec les autorisations Read, Trade et Payments depuis votre espace Deriv.',
                      style: GoogleFonts.manrope(fontSize: 13.5, height: 1.55, color: AppColors.textTertiary),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 22),
              // Label
              Padding(
                padding: const EdgeInsets.only(bottom: 8, left: 4),
                child: Text('TOKEN', style: AppTheme.labelMicro()),
              ),
              // Input
              TextField(
                controller: _controller,
                obscureText: _obscure,
                autocorrect: false,
                enableSuggestions: false,
                onChanged: (_) => setState(() {}),
                style: AppTheme.mono(fontSize: 15, color: AppColors.textPrimary, letterSpacing: 1),
                decoration: InputDecoration(
                  hintText: 'a1b2C3d4E5f6G7h8',
                  suffixIcon: TextButton(
                    onPressed: () => setState(() => _obscure = !_obscure),
                    child: Text(_obscure ? 'Voir' : 'Masquer',
                        style: GoogleFonts.manrope(color: AppColors.textTertiary, fontSize: 11, fontWeight: FontWeight.w700)),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              // Hint dot
              Row(
                children: [
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: valid ? AppColors.success : AppColors.textTertiary,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    valid ? 'Format valide — prêt à connecter' : 'Minimum 8 caractères, autorisations Read + Trade',
                    style: GoogleFonts.manrope(fontSize: 12, color: AppColors.textTertiary),
                  ),
                ],
              ),
              const SizedBox(height: 20),
              // Warning card
              Container(
                padding: const EdgeInsets.all(14),
                decoration: AppTheme.card(radius: AppRadii.md, color: AppColors.surfaceLow, border: AppColors.borderSoft),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Container(
                      width: 26,
                      height: 26,
                      decoration: BoxDecoration(
                        color: AppColors.warning.withValues(alpha: 0.14),
                        borderRadius: BorderRadius.circular(8),
                      ),
                      alignment: Alignment.center,
                      child: Text('!', style: GoogleFonts.manrope(color: AppColors.warning, fontSize: 13, fontWeight: FontWeight.w800)),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        'Le robot ne peut pas retirer de fonds. Les ordres restent limités à vos plafonds de risque.',
                        style: GoogleFonts.manrope(fontSize: 12.5, height: 1.45, color: AppColors.textSecondary),
                      ),
                    ),
                  ],
                ),
              ),
              const Spacer(),
              // CTA
              _PrimaryButton(
                label: 'Enregistrer & connecter',
                busy: _saving,
                onPressed: _saving ? null : _save,
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

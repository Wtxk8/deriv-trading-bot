import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

import '../theme/app_theme.dart';

/// Petit logo carré arrondi avec dégradé indigo — utilisé partout comme identité.
class BrandLogo extends StatelessWidget {
  const BrandLogo({super.key, this.size = 46, this.letter = 'D'});

  final double size;
  final String letter;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(size * 0.3),
        gradient: const LinearGradient(
          begin: Alignment(-0.5, -1.0),
          end: Alignment(1.0, 1.0),
          colors: [AppColors.primary, Color(0xFF3B46B8)],
        ),
        boxShadow: const [
          BoxShadow(
            color: Color(0x80535EDD),
            blurRadius: 26,
            offset: Offset(0, 10),
            spreadRadius: -8,
          ),
        ],
      ),
      alignment: Alignment.center,
      child: Text(
        letter,
        style: GoogleFonts.manrope(
          fontSize: size * 0.42,
          fontWeight: FontWeight.w800,
          color: Colors.white,
        ),
      ),
    );
  }
}

/// Chip de statut inspiré du prototype : dot pulsante + texte mono.
class StatusPill extends StatelessWidget {
  const StatusPill({super.key, required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 6),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.28), width: 1),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 6),
          Text(label, style: AppTheme.mono(fontSize: 10, fontWeight: FontWeight.w700, letterSpacing: 0.8, color: color)),
        ],
      ),
    );
  }
}

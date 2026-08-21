import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Tokens visuels de l'app (dark-first, alignés sur la maquette Axiom Bot).
abstract final class AppColors {
  static const Color bg = Color(0xFF080B10);
  static const Color surface = Color(0xFF0E131A);
  static const Color surfaceAlt = Color(0xFF10151C);
  static const Color surfaceHigh = Color(0xFF141B26);
  static const Color surfaceLow = Color(0xFF0D1218);
  static const Color border = Color(0x14FFFFFF); // white @ 8%
  static const Color borderSoft = Color(0x0FFFFFFF); // white @ 6%

  static const Color textPrimary = Color(0xFFE8EDF4);
  static const Color textSecondary = Color(0xFF9AA6B6);
  static const Color textTertiary = Color(0xFF7C8899);

  static const Color primary = Color(0xFF6C7BFF);
  static const Color primaryDeep = Color(0xFF4D59E0);
  static const Color primarySoft = Color(0xFF8B9BFF);

  static const Color success = Color(0xFF3ED598);
  static const Color danger = Color(0xFFFF5C7A);
  static const Color warning = Color(0xFFFFB247);
}

abstract final class AppRadii {
  static const double sm = 11;
  static const double md = 14;
  static const double lg = 18;
  static const double xl = 22;
  static const double pill = 999;
}

abstract final class AppSpacing {
  static const double xs = 4;
  static const double sm = 8;
  static const double md = 14;
  static const double lg = 18;
  static const double xl = 22;
  static const double xxl = 28;
}

class AppTheme {
  const AppTheme._();

  static ThemeData build() {
    final TextTheme baseText = GoogleFonts.manropeTextTheme(
      ThemeData(brightness: Brightness.dark).textTheme,
    ).apply(bodyColor: AppColors.textPrimary, displayColor: AppColors.textPrimary);

    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      scaffoldBackgroundColor: AppColors.bg,
      colorScheme: const ColorScheme.dark(
        primary: AppColors.primary,
        onPrimary: Colors.white,
        secondary: AppColors.success,
        surface: AppColors.surface,
        onSurface: AppColors.textPrimary,
        error: AppColors.danger,
      ),
      textTheme: baseText,
      appBarTheme: AppBarTheme(
        backgroundColor: AppColors.bg,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: GoogleFonts.manrope(
          fontSize: 16,
          fontWeight: FontWeight.w800,
          color: AppColors.textPrimary,
        ),
        iconTheme: const IconThemeData(color: AppColors.textPrimary),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: AppColors.surfaceAlt,
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: const BorderSide(color: AppColors.border, width: 1),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: const BorderSide(color: AppColors.border, width: 1),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: const BorderSide(color: AppColors.primary, width: 1.5),
        ),
        labelStyle: GoogleFonts.manrope(
          color: AppColors.textTertiary,
          fontWeight: FontWeight.w700,
          fontSize: 11.5,
          letterSpacing: 0.7,
        ),
        hintStyle: GoogleFonts.manrope(
          color: AppColors.textTertiary,
          fontWeight: FontWeight.w500,
        ),
      ),
      dividerColor: AppColors.border,
    );
  }

  static TextStyle mono({double? fontSize, FontWeight? fontWeight, Color? color, double? letterSpacing}) =>
      GoogleFonts.jetBrainsMono(
        fontSize: fontSize,
        fontWeight: fontWeight,
        color: color,
        letterSpacing: letterSpacing,
      );

  static TextStyle heading({double? fontSize, Color? color, double? letterSpacing}) =>
      GoogleFonts.manrope(
        fontSize: fontSize,
        fontWeight: FontWeight.w800,
        color: color ?? AppColors.textPrimary,
        letterSpacing: letterSpacing ?? -0.4,
      );

  static TextStyle labelMicro({Color? color}) => GoogleFonts.manrope(
        fontSize: 10.5,
        fontWeight: FontWeight.w700,
        letterSpacing: 0.7,
        color: color ?? AppColors.textTertiary,
      );

  static TextStyle labelSmall({Color? color}) => GoogleFonts.manrope(
        fontSize: 12,
        fontWeight: FontWeight.w700,
        color: color ?? AppColors.textTertiary,
      );

  static BoxDecoration card({double radius = AppRadii.xl, Color? color, Color? border}) => BoxDecoration(
        color: color ?? AppColors.surface,
        borderRadius: BorderRadius.circular(radius),
        border: Border.all(color: border ?? AppColors.border, width: 1),
      );

  static BoxDecoration cardGradient({double radius = AppRadii.xl}) => BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment(-0.2, -1.0),
          end: Alignment(0.5, 1.0),
          colors: [Color(0xFF18202C), Color(0xFF0E131A)],
        ),
        borderRadius: BorderRadius.circular(radius),
        border: Border.all(color: AppColors.border, width: 1),
      );
}

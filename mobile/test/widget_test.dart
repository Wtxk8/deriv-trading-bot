import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:deriv_bot_mobile/main.dart';

void main() {
  testWidgets('App boots to the API token screen when no token is stored',
      (WidgetTester tester) async {
    await tester.pumpWidget(const ProviderScope(child: DerivBotApp()));
    await tester.pumpAndSettle();

    expect(find.text('Entrez votre Token API Deriv'), findsOneWidget);
  });
}

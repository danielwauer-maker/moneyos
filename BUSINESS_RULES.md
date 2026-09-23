# MoneyOS – Business Rules

## Wirtschaftliche Ereignisse

MoneyOS unterscheidet `expense`, `income`, `transfer` und `refund`. Transfers
zwischen eigenen Konten zählen nicht als Ausgabe; Refunds sind keine normale
Einnahme. Amex-Kauf plus spätere Giro-Abrechnung erzeugt genau eine Ausgabe.

Eigene Konten: Sparda Girokonto, American Express, PayPal, Portemonnaie und Tresor.
C24 ist historisch/inaktiv. Amazon ist eine Anreicherungsquelle, kein Konto.

Technische PayPal-Zeilen (allgemeine Karten-Gutschrift/-Abbuchung,
Bankgutschrift, Autorisierung sowie Einbehalt/Rückbuchung) sind keine eigenen
Ausgaben. `Rückzahlung` ist ein Refund. Amazon-Fremdzahlungen über ein fremdes
Zahlungsmittel bleiben Information; erst eine spätere Erstattung durch den Nutzer ist
seine wirtschaftliche Ausgabe.

## Umschläge

Kategorie, physischer Umschlag und Projekt sind getrennte Dimensionen. Die zwölf
Umschläge sind Kleidung, Vergnügen, Haushalt, Mietrücklage, Urlaub, Geschenke,
Dezembergeld, Zahnarztgeld, Unterstützung Brüder, Sparen, Noée und Fix.

Bestätigte Basis zum 30.04.2026: 170, 0, 10, 375, 1.500, 100, 40, 500,
180, 1.640, 1.180 und 20 Euro (Summe 5.715 Euro in obiger Reihenfolge).

Monatlich bis 31.05.2026: Noée 50, Haushalt 25, Dezembergeld 10, Fix 626,
Geschenke 25, Sparen 100, Zahnarzt 0, Kleidung 100, Vergnügen 50, Urlaub 300,
Mietrücklage 50, Unterstützung Brüder 30 Euro. Ab 01.06.2026 ändern sich Fix auf
576, Sparen auf 0 und Mietrücklage auf 25 Euro; übrige Werte bleiben gleich.

Zahnarztgeld hat Zielbestand 500 Euro und keinen Monatsbetrag. Nach Ausgaben wird
auf 500 Euro aufgefüllt. Physischer Bestand wird stets abwärts auf volle 5 Euro
gerundet: `max(0, floor(rechnerisch/5)*5)`. Negative Rechnergebnisse werden als
Fehlbetrag getrennt gezeigt.

Umschläge sind Unterbestände des Tresors und werden nie zusätzlich zum Tresor zum
Vermögen addiert. Freies Tresorgeld = Tresor gesamt − physische Umschläge.

Abgleich: positive Deltas werden eingezahlt, negative entnommen. Transfer-Geld ist
das Minimum beider Summen. Der Restbedarf wird zuerst mit freiem Tresorgeld und
erst danach durch eine Giro-Abhebung gedeckt. Transfer-Geld ist kein Banktransfer.

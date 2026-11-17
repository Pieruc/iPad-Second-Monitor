# iPad come secondo schermo

Il PC trasmette un monitor via Wi‑Fi a Safari sull'iPad; i tocchi sull'iPad controllano il mouse.
Sull'iPad non serve installare nulla.

## Avvio
1. Doppio clic su `avvia.bat` (la prima volta installa le dipendenze).
   Se Windows chiede il permesso del firewall per Python, consenti le **reti private**.
2. **Solo la prima volta:** sull'iPad apri in Safari `http://<IP-del-PC>:8766` e segui la pagina
   per installare il certificato locale (serve per tenere lo schermo acceso; vale solo per IP locali).
   La chiave in `certs/ca.key` resta sul PC: non condividerla. Se cancelli la cartella `certs`,
   va reinstallato il certificato.
3. Apri `https://<IP-del-PC>:8765` (l'indirizzo esatto è stampato all'avvio).
4. Consigliato: Safari → Condividi → **Aggiungi alla schermata Home**, così si apre a schermo intero.

Senza certificato puoi usare `python server.py --http` (ma l'iPad andrà in standby).

PC e iPad devono essere sulla stessa rete. Se la rete (es. Wi‑Fi universitario) blocca
le connessioni tra dispositivi, attiva l'hotspot del PC e collega l'iPad a quello.

## Schermo esteso (non solo duplicato)
Windows non può creare un monitor in più senza un driver. Installa un driver di
display virtuale (es. "Virtual Display Driver" su GitHub: VirtualDrivers/Virtual-Display-Driver),
poi in Impostazioni → Schermo scegli **Estendi questi schermi** e imposta una risoluzione
4:3 come 2360x1640 o 1640x1180 per l'iPad. Il server sceglie da solo il monitor secondario.

## Gesti
- tocco = click sinistro · trascina = trascinamento · pressione lunga = click destro
- due dita = scorrimento · tre dita = mostra risoluzione/FPS
- funziona anche il trackpad della Magic Keyboard

## Opzioni
```
python server.py --list            # elenca i monitor
python server.py --monitor 2       # scegli il monitor
python server.py --fps 30 --quality 70 --scale 0.75
```
Se è a scatti, abbassa `--quality` o `--scale`.

## Via cavo USB (app nativa per iPad)
Niente Wi-Fi: l'immagine passa dal cavo tramite il servizio Apple per l'USB.

1. Sul PC installa **Apple Devices** dal Microsoft Store (o iTunes): fornisce il servizio USB.
2. L'app per iPad è in `ios/`. Si compila gratis su GitHub: scheda **Actions** →
   *Compila app iPad* → **Run workflow**; a fine compilazione scarica `SecondScreen-ipa`.
3. Installa il file `.ipa` con **Sideloadly** (firma con il tuo Apple ID; con un account
   gratuito l'app va reinstallata ogni 7 giorni). Sull'iPad attiva la **Modalità sviluppatore**
   e autorizza il tuo Apple ID in Impostazioni → Generali → VPN e gestione dispositivi.
4. Collega il cavo, avvia `avvia-usb.bat` e apri l'app **Secondo Schermo** sull'iPad.

## Pannello di controllo
Doppio clic su `Secondo Schermo.bat` (o su `pannello.pyw`): una finestra per avviare/fermare
il collegamento (cavo USB o Wi-Fi), accendere/spegnere lo schermo virtuale, cambiarne la
risoluzione, modificare le risoluzioni del driver e riavviarlo. Il pulsante
«Crea collegamento sul desktop» aggiunge un'icona per aprirlo.

## App via Wi-Fi
L'app mostra, mentre aspetta il PC, il suo **indirizzo Wi-Fi** e un **codice a 6 cifre**.
Nel pannello scegli «Wi-Fi – app Secondo Schermo» e inseriscili (oppure
`python server.py --wifi-app <IP> --code <codice>`). Il codice impedisce ad altri dispositivi
in rete di fingersi l'iPad; l'immagine però viaggia non cifrata: usalo su reti di cui ti fidi.

package io.github.jstrdev.ps5directo;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/**
 * PS5 Directo para Android: abre PS5 Directo del PC (modo movil, por la red de casa) en una ventana propia,
 * sin barra del navegador. La primera vez se conecta escaneando el QR de «Usar en el movil».
 * O, en modo «Usar sin PC», el propio PS5 Directo funcionando dentro del movil (Engine + EngineService).
 */
public class MainActivity extends Activity {
    private static final String SETUP_BASE = "https://ps5directo.app/";
    private WebView web;
    private SharedPreferences prefs;
    private PermissionRequest pendingCamera;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private int waitTicks;

    private boolean local() {
        return "local".equals(prefs.getString("mode", "pc"));
    }

    /** La direccion de PS5 Directo: la del PC (guardada al escanear el QR) o la del motor del propio movil. */
    private String home() {
        if (local()) return Engine.url == null ? "" : Engine.url;
        return prefs.getString("url", "");
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("ps5directo", MODE_PRIVATE);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0f1115"));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(s.getUserAgentString() + " PS5DirectoApp/" + BuildConfigVersion());
        CookieManager.getInstance().setAcceptCookie(true);

        web.addJavascriptInterface(new Bridge(), "PS5D");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                        request.grant(request.getResources());
                    } else {
                        pendingCamera = request;
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, 1);
                    }
                });
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame() && !req.getUrl().toString().startsWith(SETUP_BASE)) {
                    if (local()) showSetup("localerr");   // «Volver a probar» lo arranca otra vez
                    else showSetup("noconn");
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String saved = home();
                Uri home = saved.isEmpty() ? null : Uri.parse(saved);
                boolean ours = home != null && u.getHost() != null && u.getHost().equals(home.getHost())
                        && u.getPort() == home.getPort();
                if (ours || u.toString().startsWith(SETUP_BASE)) return false;
                // enlaces de fuera (GitHub, ayuda...): en el navegador del movil
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (Exception e) {
                    return false;
                }
                return true;
            }
        });
        // «Guardar en el movil» y demas descargas: a Descargas/PS5 Directo, y salen en la galeria
        web.setDownloadListener((url, ua, disposition, mime, length) -> {
            try {
                String name = URLUtil.guessFileName(url, disposition, mime);
                DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
                r.setMimeType(mime);
                r.setTitle(name);
                r.addRequestHeader("User-Agent", ua);
                r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "PS5 Directo/" + name);
                DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                dm.enqueue(r);
                Toast.makeText(this, getString(R.string.downloading, name), Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show();
            }
        });

        String url = prefs.getString("url", null);
        String shared = sharedText(getIntent());
        if (local()) startLocal(shared);
        else if (url != null && !url.isEmpty()) web.loadUrl(shared != null ? withAdd(url, shared) : url);
        else {
            if (shared != null) Toast.makeText(this, R.string.connect_first, Toast.LENGTH_LONG).show();
            showSetup("first");
        }
    }

    /** Texto compartido desde otra app (un enlace), o null. */
    private String sharedText(Intent i) {
        if (i == null || !Intent.ACTION_SEND.equals(i.getAction())) return null;
        CharSequence t = i.getCharSequenceExtra(Intent.EXTRA_TEXT);
        return t == null ? null : t.toString();
    }

    private String withAdd(String url, String text) {
        return url + (url.contains("#") ? "&" : "#") + "add=" + Uri.encode(text);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String shared = sharedText(intent);
        if (shared == null) return;
        if (local()) {
            startLocal(shared);
            return;
        }
        String url = prefs.getString("url", "");
        if (url.isEmpty()) {
            Toast.makeText(this, R.string.connect_first, Toast.LENGTH_LONG).show();
            return;
        }
        web.loadUrl(withAdd(url, shared));
    }

    /** Modo «Usar sin PC»: arranca el motor (si no lo esta) y abre PS5 Directo en cuanto responde. */
    private void startLocal(String shared) {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2);   // la notificacion fija
        }
        if (Build.VERSION.SDK_INT < 29 && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 3);   // Descargas del movil
        }
        if (Engine.url == null) Engine.error = null;   // un fallo anterior no cuenta: se vuelve a probar
        Intent svc = new Intent(this, EngineService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
        else startService(svc);
        if (Engine.url != null) {
            web.loadUrl(shared != null ? withAdd(Engine.url, shared) : Engine.url);
            return;
        }
        showSetup("starting");
        waitTicks = 0;
        ui.removeCallbacksAndMessages(null);
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (Engine.url != null) {
                    web.loadUrl(shared != null ? withAdd(Engine.url, shared) : Engine.url);
                } else if (Engine.error != null || ++waitTicks > 200) {   // 200 x 300 ms = 1 minuto
                    showSetup("localerr");
                } else {
                    ui.postDelayed(this, 300);
                }
            }
        }, 300);
    }

    /** Wake-on-LAN: el «paquete mágico» a la red de casa para despertar el PC (si su tarjeta lo permite). */
    private void sendWake() {
        String mac = prefs.getString("mac", "");
        String bc = prefs.getString("bcast", "");
        if (mac.isEmpty()) return;
        new Thread(() -> {
            try {
                String[] hex = mac.split("[:-]");
                byte[] m = new byte[6];
                for (int i = 0; i < 6; i++) m[i] = (byte) Integer.parseInt(hex[i], 16);
                byte[] pkt = new byte[6 + 16 * 6];
                for (int i = 0; i < 6; i++) pkt[i] = (byte) 0xff;
                for (int i = 6; i < pkt.length; i += 6) System.arraycopy(m, 0, pkt, i, 6);
                DatagramSocket sock = new DatagramSocket();
                sock.setBroadcast(true);
                String[] targets = bc.isEmpty() ? new String[]{"255.255.255.255"} : new String[]{bc, "255.255.255.255"};
                for (String t : targets) {
                    for (int port : new int[]{9, 7}) {
                        sock.send(new DatagramPacket(pkt, pkt.length, InetAddress.getByName(t), port));
                    }
                }
                sock.close();
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show());
            }
        }).start();
    }

    private String BuildConfigVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "1";
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        if (code != 1 || pendingCamera == null) return;
        if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) pendingCamera.grant(pendingCamera.getResources());
        else pendingCamera.deny();
        pendingCamera = null;
    }

    private void showSetup(String mode) {
        try {
            InputStream in = getAssets().open("setup.html");
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            in.close();
            String html = out.toString(StandardCharsets.UTF_8.name())
                    .replace("{{MODE}}", mode)
                    .replace("{{URL}}", prefs.getString("url", "").replace("\"", "").replace("<", ""))
                    .replace("{{ERR}}", String.valueOf(Engine.error == null ? "" : Engine.error).replace("\"", "'").replace("<", "").replace("\n", " "));
            web.loadDataWithBaseURL(SETUP_BASE, html, "text/html", "utf-8", null);
        } catch (Exception e) {
            Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    /** Lo que la pagina puede pedir a la app. */
    private class Bridge {
        @JavascriptInterface
        public void save(String url) {
            if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) return;
            prefs.edit().putString("url", url).putString("mode", "pc").apply();
            runOnUiThread(() -> web.loadUrl(url));
        }

        @JavascriptInterface
        public void retry() {
            String url = prefs.getString("url", "");
            runOnUiThread(() -> {
                if (local()) startLocal(null);
                else if (url.isEmpty()) showSetup("first");
                else web.loadUrl(url);
            });
        }

        @JavascriptInterface
        public void reset() {
            boolean wasLocal = local();
            prefs.edit().remove("url").putString("mode", "pc").apply();
            runOnUiThread(() -> {
                showSetup("first");
                // fuera la notificacion fija; el motor se queda quieto hasta cerrar la app
                if (wasLocal) stopService(new Intent(MainActivity.this, EngineService.class));
            });
        }

        /** «Usar sin PC»: PS5 Directo funcionando dentro del propio movil. */
        @JavascriptInterface
        public void useLocal() {
            prefs.edit().putString("mode", "local").apply();
            runOnUiThread(() -> startLocal(null));
        }

        @JavascriptInterface
        public boolean isLocal() {
            return local();
        }

        @JavascriptInterface
        public boolean isApp() {
            return true;
        }

        /** La pagina de PS5 Directo nos dice como despertar ese PC (su MAC y la red). */
        @JavascriptInterface
        public void saveWake(String mac, String bcast) {
            if (mac == null || !mac.matches("(?i)[0-9a-f]{2}([:-][0-9a-f]{2}){5}")) return;
            prefs.edit().putString("mac", mac).putString("bcast", bcast == null ? "" : bcast).apply();
        }

        @JavascriptInterface
        public boolean canWake() {
            return !prefs.getString("mac", "").isEmpty();
        }

        @JavascriptInterface
        public void wake() {
            sendWake();
        }
    }
}

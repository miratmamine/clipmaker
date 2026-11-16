package com.clipmaker.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.DownloadListener;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.Toast;

/**
 * Coquille WebView : affiche l'interface web ClipMaker servie par le PC.
 *
 * L'adresse du serveur est saisie en haut de l'écran puis mémorisée.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "clipmaker";
    private static final String KEY_SERVER = "server_url";
    private static final String DEFAULT_SERVER = "http://192.168.1.10:5000";

    private EditText serverField;
    private WebView webView;
    private ProgressBar progressBar;
    private FrameLayout root;
    private View customView;
    private WebChromeClient.CustomViewCallback customViewCallback;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        root = findViewById(R.id.root);
        serverField = findViewById(R.id.server_url);
        progressBar = findViewById(R.id.progress);
        webView = findViewById(R.id.webview);
        Button goButton = findViewById(R.id.go);
        Button reloadButton = findViewById(R.id.reload);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        webView.setBackgroundColor(Color.parseColor("#0b1020"));

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progressBar.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
                progressBar.setProgress(newProgress);
            }

            @Override
            public void onShowCustomView(View view, CustomViewCallback callback) {
                if (customView != null) {
                    callback.onCustomViewHidden();
                    return;
                }
                customView = view;
                customViewCallback = callback;
                root.addView(view, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
                webView.setVisibility(View.GONE);
            }

            @Override
            public void onHideCustomView() {
                exitFullscreen();
            }
        });

        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception exc) {
                    Toast.makeText(MainActivity.this,
                            "Impossible d'ouvrir le lien : " + exc.getMessage(),
                            Toast.LENGTH_LONG).show();
                }
            }
        });

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String saved = prefs.getString(KEY_SERVER, "");
        if (saved.isEmpty()) {
            serverField.setHint(DEFAULT_SERVER);
            webView.loadDataWithBaseURL(null, WELCOME_HTML, "text/html", "utf-8", null);
        } else {
            serverField.setText(saved);
            webView.loadUrl(saved);
        }

        goButton.setOnClickListener(v -> openServer());
        reloadButton.setOnClickListener(v -> webView.reload());
        serverField.setOnEditorActionListener((v, actionId, event) -> {
            if (actionId == EditorInfo.IME_ACTION_GO || actionId == EditorInfo.IME_ACTION_DONE) {
                openServer();
                return true;
            }
            return false;
        });
    }

    private void openServer() {
        String url = serverField.getText().toString().trim();
        if (url.isEmpty()) {
            Toast.makeText(this, "Saisis l'adresse du PC, par exemple " + DEFAULT_SERVER,
                    Toast.LENGTH_LONG).show();
            return;
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        serverField.setText(url);
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_SERVER, url).apply();
        webView.loadUrl(url);
    }

    private void exitFullscreen() {
        if (customView == null) {
            return;
        }
        root.removeView(customView);
        customView = null;
        webView.setVisibility(View.VISIBLE);
        if (customViewCallback != null) {
            customViewCallback.onCustomViewHidden();
            customViewCallback = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (customView != null) {
            exitFullscreen();
            return;
        }
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }

    private static final String WELCOME_HTML = "<!doctype html><html lang=\"fr\"><head>"
            + "<meta charset=\"utf-8\">"
            + "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            + "<style>"
            + "body{margin:0;padding:24px;background:#0b1020;color:#e8ecf8;"
            + "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.55}"
            + "h1{font-size:1.5rem;margin:0 0 4px}"
            + ".sub{color:#b8c4e6;margin:0 0 20px;font-size:.92rem}"
            + ".card{background:#141a2e;border:1px solid #26304d;border-radius:14px;padding:16px;margin-bottom:14px}"
            + "h2{font-size:1.05rem;margin:0 0 10px}"
            + "ol{margin:0;padding-left:22px}li{margin-bottom:8px}"
            + "code{background:#0d1326;border-radius:6px;padding:2px 6px;color:#9db4ff}"
            + ".hint{color:#8f9bb8;font-size:.85rem}"
            + "</style></head><body>"
            + "<h1>ClipMaker</h1>"
            + "<p class=\"sub\">Clips verticaux sous-titres, generes sur ton PC.</p>"
            + "<div class=\"card\"><h2>1. Lance le serveur sur le PC</h2>"
            + "<p>Dans le dossier <code>clipmaker</code> :</p>"
            + "<p><code>python webapp.py</code></p>"
            + "<p class=\"hint\">Le terminal affiche une adresse du type "
            + "<code>http://192.168.1.10:5000</code>.</p></div>"
            + "<div class=\"card\"><h2>2. Saisis cette adresse en haut</h2>"
            + "<p>Appuie sur <b>Aller</b>. Le telephone doit etre sur le meme Wi-Fi "
            + "que le PC.</p></div>"
            + "<div class=\"card\"><h2>3. Genere des clips</h2>"
            + "<p>Colle une URL YouTube, choisis le nombre de clips, puis lance le "
            + "traitement. La progression s'affiche en direct et les clips se lisent "
            + "dans l'application.</p></div>"
            + "<p class=\"hint\">Si la page ne charge pas : verifie l'adresse, le Wi-Fi, "
            + "et autorise le port 5000 dans le pare-feu Windows.</p>"
            + "</body></html>";
}

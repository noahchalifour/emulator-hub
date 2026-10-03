package dev.emulatorhub.e2e;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.text.Editable;
import android.text.TextWatcher;
import android.util.Log;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.EditText;
import android.widget.FrameLayout;

/**
 * Input probe for the emulator-hub end-to-end tests. Logs, under tag "E2E":
 *   touch <action> <rawX> <rawY>   for every touch (display pixels)
 *   key <keycode> <action>         for every key that reaches the window
 *   text <full field content>      whenever the field changes
 * The screen colour flips on every touch so frame streams see a change.
 */
public class ProbeActivity extends Activity {
    private static final String TAG = "E2E";
    private FrameLayout root;
    private boolean dark;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);
        EditText field = new EditText(this);
        field.setSingleLine(false);
        field.setLayoutParams(new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        field.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {}
            public void onTextChanged(CharSequence s, int a, int b, int c) {}
            public void afterTextChanged(Editable s) {
                Log.i(TAG, "text " + s.toString());
            }
        });
        root.addView(field);
        setContentView(root);
        field.requestFocus();
        Log.i(TAG, "ready");
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        Log.i(TAG, "touch " + ev.getActionMasked() + " " + Math.round(ev.getRawX()) + " " + Math.round(ev.getRawY()));
        if (ev.getActionMasked() == MotionEvent.ACTION_DOWN) {
            dark = !dark;
            root.setBackgroundColor(dark ? Color.DKGRAY : Color.WHITE);
        }
        return super.dispatchTouchEvent(ev);
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent ev) {
        Log.i(TAG, "key " + ev.getKeyCode() + " " + ev.getAction());
        return super.dispatchKeyEvent(ev);
    }
}

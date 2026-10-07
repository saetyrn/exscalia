// Thin shell around the EXSCALIA web frontend: registers the storage
// plugins and exposes the OS keychain for the API bearer token, which must
// not live in webview storage.

const KEYCHAIN_SERVICE: &str = "art.exscalia.desktop";
const KEYCHAIN_ACCOUNT: &str = "api-token";

fn keychain_entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT).map_err(|e| e.to_string())
}

#[tauri::command]
fn secret_get() -> Result<Option<String>, String> {
    match keychain_entry()?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn secret_set(value: String) -> Result<(), String> {
    keychain_entry()?.set_password(&value).map_err(|e| e.to_string())
}

#[tauri::command]
fn secret_delete() -> Result<(), String> {
    match keychain_entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_sql::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        // The webview must never leave the app. Rendered markdown marks
        // off-site links target="_blank" so the opener plugin above sends
        // them to the system browser, but any anchor that slips past would
        // otherwise replace EXSCALIA in its only window, which has no back
        // control. Allow the app's own origins and cancel the rest:
        // tauri://localhost (macOS), http://tauri.localhost (Windows) and the
        // dev server on localhost.
        .plugin(
            tauri::plugin::Builder::<tauri::Wry>::new("navguard")
                .on_navigation(|_webview, url| {
                    url.scheme() == "tauri"
                        || matches!(
                            url.host_str(),
                            Some("tauri.localhost") | Some("localhost") | Some("127.0.0.1")
                        )
                })
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .invoke_handler(tauri::generate_handler![
            secret_get,
            secret_set,
            secret_delete
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

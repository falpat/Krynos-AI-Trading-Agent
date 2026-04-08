use std::env;
use std::process;

use clap::Parser;

use kraken_cli::errors::KrakenError;
use kraken_cli::output::OutputFormat;
use kraken_cli::{client, AppContext, Cli};

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    let format = cli.output.unwrap_or(OutputFormat::Table);

    // Determine secret and track whether it came from the raw --api-secret flag
    let (api_secret, secret_from_flag) = if cli.api_secret_stdin {
        match kraken_cli::config::read_secret_from_stdin() {
            Ok(s) => (Some(s.expose().to_string()), false),
            Err(e) => {
                kraken_cli::output::render_error(format, &e);
                process::exit(1);
            }
        }
    } else if let Some(ref path) = cli.api_secret_file {
        match kraken_cli::config::read_secret_from_file(path) {
            Ok(s) => (Some(s.expose().to_string()), false),
            Err(e) => {
                kraken_cli::output::render_error(format, &e);
                process::exit(1);
            }
        }
    } else if cli.api_secret.is_some() {
        (cli.api_secret.clone(), true)
    } else {
        (None, false)
    };

    // OTP: flag > env
    let otp = cli.otp.clone().or_else(|| env::var("KRAKEN_OTP").ok());

    // Emit secret-exposure warning centrally for both Spot and Futures paths
    if secret_from_flag {
        kraken_cli::output::warn(
            "Passing --api-secret on the command line exposes it in process listings. \
             Prefer --api-secret-stdin, --api-secret-file, or environment variables.",
        );
    }

    // Resolve URL overrides: CLI flag > env var > default
    let resolve = |cli_flag: Option<&str>, env_name: &str, source_label: &str| -> Option<String> {
        let env_val = env::var(env_name).ok();
        match client::resolve_url_override(cli_flag, env_val.as_deref()) {
            Ok(url) => url,
            Err(e) => {
                let source = if cli_flag.is_some() {
                    source_label
                } else {
                    env_name
                };
                let inner = match &e {
                    KrakenError::Validation(msg) => msg.clone(),
                    other => other.to_string(),
                };
                kraken_cli::output::render_error(
                    format,
                    &KrakenError::Validation(format!("{source}: {inner}")),
                );
                process::exit(1);
            }
        }
    };

    let api_url = resolve(cli.api_url.as_deref(), "KRAKEN_SPOT_URL", "--api-url");
    let futures_url = resolve(
        cli.futures_url.as_deref(),
        "KRAKEN_FUTURES_URL",
        "--futures-url",
    );
    let ws_public_url = resolve(None, "KRAKEN_WS_PUBLIC_URL", "KRAKEN_WS_PUBLIC_URL");
    let ws_auth_url = resolve(None, "KRAKEN_WS_AUTH_URL", "KRAKEN_WS_AUTH_URL");
    let ws_l3_url = resolve(None, "KRAKEN_WS_L3_URL", "KRAKEN_WS_L3_URL");

    let ctx = AppContext {
        format,
        verbose: cli.verbose,
        api_url,
        futures_url,
        ws_public_url,
        ws_auth_url,
        ws_l3_url,
        api_key: cli.api_key.clone(),
        api_secret,
        otp,
        force: cli.yes,
        secret_from_flag,
        mcp_mode: false,
    };

    match cli.command {
        Some(command) => {
            if let Err(e) = kraken_cli::dispatch(&ctx, command).await {
                kraken_cli::output::render_error(ctx.format, &e);
                process::exit(1);
            }
        }
        None => {
            use clap::CommandFactory;
            Cli::command().print_help().ok();
            println!();
        }
    }
}

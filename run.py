import subprocess
from pyngrok import ngrok, conf

# Forzi pyngrok a usare l'eseguibile installato direttamente nel sistema
conf.get_default().ngrok_path = r"C:\Program Files\ngrok\ngrok.exe"  # o il percorso dove installa winget

def avvia_app():
    tunnel_pubblico = ngrok.connect(8501)
    print("\n" + "=" * 60)
    print(f"🚀 APP ONLINE: {tunnel_pubblico.public_url}")
    print("=" * 60 + "\n")

    cmd = ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
    subprocess.run(cmd)

if __name__ == "__main__":
    avvia_app()
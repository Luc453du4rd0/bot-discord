using System;
using System.Windows.Forms;
using KeyAuth;

namespace Painel
{
    public partial class Form1 : Form
    {

        Timer timerErro = new Timer();

        public Form1()
        {
            InitializeComponent();
            KeyAuthApp.init();
        }

        public static api KeyAuthApp = new api(
    name: "Eulucaxs1302's Application", // App name
    ownerid: "RIngkRXLmZ", // Account ID
    secret: "2c72415194320abc872e72d9152f42566934ae1e1c8419a7bafe122b4c982f45",
    version: "1.0" // Application version. Used for automatic downloads see video here https://www.youtube.com/watch?v=kW195PLCBKs
                   //path: @"Your_Path_Here" // (OPTIONAL) see tutorial here https://www.youtube.com/watch?v=I9rxt82IgMk&t=1s
);


        private void Form1_Load(object sender, EventArgs e)
        {
            KeyAuthApp.init();

            if (!KeyAuthApp.response.success)
            {
                MessageBox.Show(KeyAuthApp.response.message, "Erro", MessageBoxButtons.OK, MessageBoxIcon.Error);
                this.Close();
            }
        }

        private void label1_Click(object sender, EventArgs e)
        {
        }

        private void guna2CirclePictureBox1_Click(object sender, EventArgs e)
        {
        }

        private void lczxy7AnimatedButton11_Click(object sender, EventArgs e)
        {
            labelErro.Visible = false;
            labelErro.Text = "";

            // botão vira carregando
            lczxy7AnimatedButton11.Text = "Carregando...";
            lczxy7AnimatedButton11.Enabled = false;

            if (string.IsNullOrWhiteSpace(Username.Text) || string.IsNullOrWhiteSpace(Pass.Text))
            {
                labelErro.Text = "Preencha todos os campos";
                labelErro.Visible = true;
                timerErro.Start();

                // volta botão ao normal
                lczxy7AnimatedButton11.Text = "Entrar";
                lczxy7AnimatedButton11.Enabled = true;

                return;
            }

            KeyAuthApp.login(Username.Text, Pass.Text);

            if (KeyAuthApp.response.success)
            {
                Form2 main = new Form2();
                main.StartPosition = FormStartPosition.CenterScreen;
                main.Show();
                this.Hide();
            }
            else
            {
                string erro = (KeyAuthApp.response.message ?? "").ToLower();

                if (erro.Contains("expired") || erro.Contains("expir"))
                {
                    labelErro.Text = "Acesso expirado";
                }
                else
                {
                    labelErro.Text = "Usuário ou senha inválidos";
                }

                labelErro.Visible = true;
                timerErro.Start();

                lczxy7AnimatedButton11.Text = "Entrar";
                lczxy7AnimatedButton11.Enabled = trUe;
            }
        }

    }
}

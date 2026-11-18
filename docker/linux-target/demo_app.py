"""A small 'legacy business app' to automate: no accessibility, no ids - only pixels.

Login form -> main screen with a 60-row order grid (scrolls; Ctrl+A / Ctrl+C copy it as
tab-separated text like a JTable), a checkbox, and an export with dialogs that come in
a variable order (the 'replace?' question only from the 2nd export).
"""
import tkinter as tk
from tkinter import ttk

FONT = ("Liberation Sans", 11)
CLIENTS = ["Dupont", "Lefèvre", "Martin", "Bernard", "Thomas", "Petit", "Robert", "Richard"]
ROWS = [(f"CMD-{i:04d}", CLIENTS[i % len(CLIENTS)], f"{(i * 37) % 900 + 50},{i % 100:02d}",
         ["Reçu", "En cours", "Livré"][i % 3]) for i in range(1, 61)]

root = tk.Tk()
root.title("Gestion des commandes")
root.geometry("900x620+0+0")
root.option_add("*Font", FONT)
style = ttk.Style()
style.theme_use("clam")
style.configure("Treeview", font=FONT, rowheight=24)
style.configure("Treeview.Heading", font=(FONT[0], 11, "bold"))
exports = {"n": 0}


def dialog(title, message, buttons, on_close=None):
    d = tk.Toplevel(root)
    d.title(title)
    d.geometry("+300+250")
    d.transient(root)
    tk.Label(d, text=message, padx=30, pady=20).pack()
    row = tk.Frame(d)
    row.pack(pady=(0, 15))
    for b in buttons:
        def press(b=b):
            d.destroy()
            if on_close:
                root.after(300, lambda: on_close(b))
        tk.Button(row, text=b, width=8, command=press).pack(side="left", padx=6)


def export():
    exports["n"] += 1

    def done(_):
        dialog("Export", "Export terminé : 60 lignes", ["OK"])

    if exports["n"] > 1:
        dialog("Export", "Le fichier existe déjà. Remplacer ?", ["Oui", "Non"], on_close=done)
    else:
        root.after(400, lambda: done(None))


def main_screen(user):
    login.destroy()
    # menu bar: "Fichier > Exporter > CSV" - same text as the "Exporter" button on purpose
    bar = tk.Menu(root)
    fichier = tk.Menu(bar, tearoff=0)
    exporter = tk.Menu(fichier, tearoff=0)
    exporter.add_command(label="CSV", command=lambda: dialog("Export", "Export CSV lancé", ["OK"]))
    exporter.add_command(label="PDF", command=lambda: dialog("Export", "Export PDF lancé", ["OK"]))
    fichier.add_cascade(label="Exporter", menu=exporter)
    fichier.add_separator()
    fichier.add_command(label="Quitter", command=root.destroy)
    bar.add_cascade(label="Fichier", menu=fichier)
    bar.add_cascade(label="Aide", menu=tk.Menu(bar, tearoff=0))
    root.config(menu=bar)
    top = tk.Frame(root, padx=12, pady=10)
    top.pack(fill="x")
    tk.Label(top, text=f"Bienvenue {user}").pack(side="left")
    tk.Label(top, text="Pays :").pack(side="left", padx=(30, 4))
    pays = ttk.Combobox(top, values=["Belgique", "Canada", "France", "Luxembourg", "Maroc",
                                     "Suisse", "Tunisie"], width=14, state="readonly")
    pays.pack(side="left")
    choix = tk.Label(top, text="Pays choisi : aucun")
    choix.pack(side="left", padx=10)
    pays.bind("<<ComboboxSelected>>", lambda _e: choix.config(text=f"Pays choisi : {pays.get()}"))
    tk.Button(top, text="Exporter", command=export).pack(side="right")
    actif = tk.BooleanVar(value=True)
    tk.Checkbutton(top, text="Actif", variable=actif).pack(side="right", padx=20)
    status = tk.Label(root, text="Aucune commande sélectionnée", anchor="w", padx=12)
    status.pack(fill="x", side="bottom", pady=6)
    frame = tk.Frame(root, padx=12)
    frame.pack(fill="both", expand=True)
    cols = ("Numéro", "Client", "Montant", "Statut")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=18)
    for c, w in zip(cols, (140, 200, 120, 140)):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="w")
    for r in ROWS:
        tree.insert("", "end", values=r)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def select_all(_=None):
        tree.selection_set(tree.get_children())
        return "break"

    def copy(_=None):
        sel = tree.selection() or tree.get_children()
        lines = ["\t".join(cols)] + ["\t".join(str(v) for v in tree.item(i, "values")) for i in sel]
        root.clipboard_clear()
        root.clipboard_append("\n".join(lines))
        return "break"

    def on_select(_=None):
        sel = tree.selection()
        if len(sel) == 1:
            status.config(text=f"Commande {tree.item(sel[0], 'values')[0]} sélectionnée")

    def on_open(_=None):
        sel = tree.selection()
        if sel:
            v = tree.item(sel[0], "values")
            dialog("Détail", f"Commande {v[0]} - {v[1]} - {v[2]} EUR", ["Fermer"])

    # like real grids (JTable, DataGridView): a click gives the grid the keyboard focus
    tree.bind("<ButtonPress-1>", lambda _e: tree.focus_set(), add="+")
    tree.bind("<Control-a>", select_all)
    tree.bind("<Control-c>", copy)
    tree.bind("<<TreeviewSelect>>", on_select)
    tree.bind("<Double-1>", on_open)


login = tk.Frame(root, padx=40, pady=40)
login.pack(fill="both", expand=True)
tk.Label(login, text="Connexion", font=(FONT[0], 16, "bold")).grid(row=0, column=0, columnspan=2,
                                                                   pady=(0, 20), sticky="w")
tk.Label(login, text="Utilisateur :").grid(row=1, column=0, sticky="w", pady=6)
user_e = tk.Entry(login, width=28)
user_e.grid(row=1, column=1, pady=6)
tk.Label(login, text="Mot de passe :").grid(row=2, column=0, sticky="w", pady=6)
pass_e = tk.Entry(login, width=28, show="*")
pass_e.grid(row=2, column=1, pady=6)
msg = tk.Label(login, text="", fg="#b00020")
msg.grid(row=4, column=0, columnspan=2, sticky="w")


def submit(_=None):
    if pass_e.get() != "secret":
        msg.config(text="Mot de passe incorrect")
        return
    root.after(600, lambda: main_screen(user_e.get()))   # a slow app: the screen comes later


tk.Button(login, text="Valider", width=10, command=submit, bg="#1e64c8", fg="white",
          activebackground="#174ea0", activeforeground="white").grid(row=3, column=1,
                                                                    sticky="e", pady=16)
root.mainloop()

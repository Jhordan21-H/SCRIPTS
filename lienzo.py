# -*- coding: utf-8 -*-
# lienzo_dtf.py
#
# pip install pillow
#
# Lienzo para DTF:
# - Presets de hoja en cm, DPI y espejo en exportación
# - PNG con fondo realmente transparente (alpha=0)
# - Insertar imágenes, mover, escalar, rotar
# - Mantener proporción: al cambiar Ancho/Alto (cm) el otro se actualiza automáticamente
# - Zoom de vista (Ctrl+rueda, 100%, Ajustar) y paneo (botón medio / Space+arrastrar)

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageOps

CM_PER_INCH = 2.54
def cm_to_px(cm, dpi): return int(round(cm / CM_PER_INCH * dpi))
def px_to_cm(px, dpi): return (px / dpi) * CM_PER_INCH

# ------------------ Capa con caché ------------------
class LayerImage:
    def __init__(self, pil_image, x, y, scale=1.0, angle=0.0, opacity=1.0):
        self.pil_original = pil_image.convert("RGBA")
        self.scale = scale
        self.angle = angle
        self.opacity = opacity
        self.x = x; self.y = y
        self.canvas_id = None
        self.locked = False
        self.name = os.path.basename(getattr(pil_image, "filename", "imagen"))
        self._cache = {}       # (disp_scale, angle, opacity) -> (PIL, TK)
        self._last_tk = None   # evita GC

    def _key(self, disp_scale):
        return (round(disp_scale, 4), round(self.angle, 2), round(self.opacity, 2))

    def render_cached(self, disp_scale):
        k = self._key(disp_scale)
        if k in self._cache:
            return self._cache[k]
        w, h = self.pil_original.size
        new_w = max(1, int(round(w * disp_scale)))
        new_h = max(1, int(round(h * disp_scale)))
        img = self.pil_original.resize((new_w, new_h), Image.LANCZOS)
        if self.opacity < 1.0:
            a = img.getchannel("A").point(lambda p: int(p * self.opacity))
            img.putalpha(a)
        if abs(self.angle) > 0.001:
            img = img.rotate(-self.angle, resample=Image.BICUBIC, expand=True)
        tkimg = ImageTk.PhotoImage(img)
        self._cache[k] = (img, tkimg)
        return img, tkimg

    def clear_cache(self): self._cache.clear()

    def bounds(self):
        img, _ = self.render_cached(self.scale)
        return (self.x, self.y, self.x + img.width, self.y + img.height)

# ------------------ App ------------------
class TransparentCanvasApp:
    GRID_CM = 1.0
    SNAP_CM = 0.5

    def __init__(self, root):
        self.root = root
        root.title("Lienzo DTF (cm → impresión real)")

        # Estado de lienzo / DTF
        self.dpi = tk.IntVar(value=300)
        self.width_cm = tk.DoubleVar(value=33.0)
        self.height_cm = tk.DoubleVar(value=48.0)
        self.mirror_on_export = tk.BooleanVar(value=True)

        # Vista
        self.view_scale = 1.0
        self.min_view, self.max_view = 0.08, 8.0
        self._panning = False
        self._pan_start = (0, 0)

        # Opciones visuales
        self.show_grid = tk.BooleanVar(value=True)
        self.show_checker = tk.BooleanVar(value=True)
        self.snap_to_grid = tk.BooleanVar(value=True)
        self.rulers_on = tk.BooleanVar(value=True)
        self.fast_drag = tk.BooleanVar(value=True)

        # Datos
        self.images = []
               # capa seleccionada
        self.selected = None
        self.drag_offset = (0, 0)
        self.canvas_px = (0, 0)
        self.selection_rect = None
        self._checker_tk = None

        # UI
        self._build_menubar()
        self._build_toolbar()

        main = ttk.Frame(root); main.pack(fill=tk.BOTH, expand=True)

        self.left_panel = self._build_left_panel(main)
        self.left_panel.pack(side=tk.LEFT, fill=tk.Y)

        center = ttk.Frame(main); center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.ruler_top = tk.Canvas(center, height=24, bg="#f4f4f4", highlightthickness=0)
        self.ruler_top.pack(side=tk.TOP, fill=tk.X)

        mid = ttk.Frame(center); mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.ruler_left = tk.Canvas(mid, width=28, bg="#f4f4f4", highlightthickness=0)
        self.ruler_left.pack(side=tk.LEFT, fill=tk.Y)

        self.scroll_y = tk.Scrollbar(mid, orient=tk.VERTICAL)
        self.scroll_x = tk.Scrollbar(center, orient=tk.HORIZONTAL)

        self.canvas = tk.Canvas(
            mid, bg="#bdbdbd", highlightthickness=0,
            xscrollcommand=self.scroll_x.set, yscrollcommand=self.scroll_y.set
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scroll_y.config(command=self.canvas.yview); self.scroll_x.config(command=self.canvas.xview)
        self.scroll_y.pack(side=tk.RIGHT, fill=tk.Y); self.scroll_x.pack(side=tk.BOTTOM, fill=tk.X)

        self.status = ttk.Label(root, text="Listo", anchor="w"); self.status.pack(side=tk.BOTTOM, fill=tk.X)

        # Eventos
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_motion)  # <- asegurado
        # Zoom de vista (Ctrl+rueda)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)  # Linux up
        self.canvas.bind("<Button-5>", self.on_mousewheel)  # Linux down
        # Manipular capa: rueda (sin Ctrl; Shift=rotar)
        root.bind("<MouseWheel>", self.on_wheel_selected, add="+")
        root.bind("<Button-4>", self.on_wheel_selected, add="+")
        root.bind("<Button-5>", self.on_wheel_selected, add="+")
        root.bind("<Delete>", self.delete_selected)
        root.bind("+", lambda e: self.bump_scale(1.05))
        root.bind("-", lambda e: self.bump_scale(1/1.05))
        root.bind("q", lambda e: self.nudge_rotation(-5))
        root.bind("e", lambda e: self.nudge_rotation(+5))
        root.bind("a", lambda e: self.nudge_rotation(-1))
        root.bind("d", lambda e: self.nudge_rotation(+1))
        root.bind("<Control-o>", lambda e: self.menu_add_images())
        root.bind("<Control-s>", lambda e: self.menu_export_png())
        root.bind("<Control-f>", lambda e: self.bring_to_front())
        root.bind("<Control-b>", lambda e: self.send_to_back())
        # Paneo
        self.canvas.bind("<Button-2>", self.start_pan)
        self.canvas.bind("<B2-Motion>", self.do_pan)
        self.canvas.bind("<ButtonRelease-2>", self.stop_pan)
        root.bind("<KeyPress-space>", lambda e: setattr(self, "_panning", True))
        root.bind("<KeyRelease-space>", lambda e: setattr(self, "_panning", False))
        # Menú contextual
        self.ctx_menu = tk.Menu(self.canvas, tearoff=0)
        self.ctx_menu.add_command(label="Traer al frente", command=self.bring_to_front)
        self.ctx_menu.add_command(label="Enviar al fondo", command=self.send_to_back)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="Bloquear/Desbloquear", command=self.toggle_lock)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="Eliminar", command=self.delete_selected)
        self.canvas.bind("<Button-3>", self.on_right_click)

        # Inicial
        self.apply_size()
        root.minsize(1000, 650)
        self.fit_to_window()

    # ---------- UI ----------
    def _build_menubar(self):
        mb = tk.Menu(self.root); self.root.config(menu=mb)
        mfile = tk.Menu(mb, tearoff=0)
        mfile.add_command(label="Nuevo lienzo", command=self.new_canvas)
        mfile.add_command(label="Abrir imágenes…", command=self.menu_add_images, accelerator="Ctrl+O")
        mfile.add_separator()
        mfile.add_command(label="Exportar PNG (transparente)…", command=self.menu_export_png, accelerator="Ctrl+S")
        mfile.add_separator()
        mfile.add_command(label="Salir", command=self.root.quit)
        mb.add_cascade(label="Archivo", menu=mfile)

        medit = tk.Menu(mb, tearoff=0)
        medit.add_command(label="Traer al frente", command=self.bring_to_front, accelerator="Ctrl+F")
        medit.add_command(label="Enviar al fondo", command=self.send_to_back, accelerator="Ctrl+B")
        medit.add_command(label="Bloquear/Desbloquear", command=self.toggle_lock)
        medit.add_separator()
        medit.add_command(label="Eliminar", command=self.delete_selected, accelerator="Supr")
        mb.add_cascade(label="Editar", menu=medit)

        mview = tk.Menu(mb, tearoff=0)
        mview.add_checkbutton(label="Cuadrícula (cm)", variable=self.show_grid, command=self.redraw_all)
        mview.add_checkbutton(label="Checker (solo guía)", variable=self.show_checker, command=self.redraw_all)
        mview.add_checkbutton(label="Rulers", variable=self.rulers_on, command=self.redraw_all)
        mview.add_checkbutton(label="Snap a cuadrícula", variable=self.snap_to_grid)
        mview.add_checkbutton(label="Modo rápido al arrastrar", variable=self.fast_drag)
        mb.add_cascade(label="Ver", menu=mview)

    def _build_toolbar(self):
        tb = ttk.Frame(self.root, padding=(6, 4)); tb.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(tb, text="Nuevo", command=self.new_canvas).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb, text="Abrir imágenes", command=self.menu_add_images).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb, text="Exportar PNG", command=self.menu_export_png).pack(side=tk.LEFT, padx=2)
        ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Checkbutton(tb, text="Espejar al exportar (DTF)", variable=self.mirror_on_export).pack(side=tk.LEFT, padx=(0,8))

        ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(tb, text="–", width=3, command=lambda: self.zoom_view(step=-1)).pack(side=tk.LEFT)
        ttk.Button(tb, text="+", width=3, command=lambda: self.zoom_view(step=+1)).pack(side=tk.LEFT)
        ttk.Button(tb, text="100%", command=lambda: self.set_view_scale(1.0)).pack(side=tk.LEFT, padx=(6,2))
        ttk.Button(tb, text="Ajustar", command=self.fit_to_window).pack(side=tk.LEFT)

    def _build_left_panel(self, parent):
        panel = ttk.Frame(parent, padding=8)

        # --- Presets DTF ---
        ttk.Label(panel, text="Hoja DTF (cm)").pack(anchor="w")
        p = ttk.Frame(panel); p.pack(fill=tk.X, pady=4)
        self.preset = tk.StringVar(value="33×48")
        presets = ["33×48", "35×45", "30×50", "A3 (29.7×42)", "A4 (21×29.7)", "Personalizado"]
        cb = ttk.Combobox(p, values=presets, textvariable=self.preset, state="readonly", width=18)
        cb.grid(row=0, column=0, columnspan=2, sticky="we"); cb.bind("<<ComboboxSelected>>", self.apply_preset)

        ttk.Label(p, text="Ancho").grid(row=1, column=0, sticky="w")
        self.ent_w = ttk.Entry(p, width=8, textvariable=self.width_cm); self.ent_w.grid(row=1, column=1, padx=4)
        ttk.Label(p, text="Alto").grid(row=2, column=0, sticky="w")
        self.ent_h = ttk.Entry(p, width=8, textvariable=self.height_cm); self.ent_h.grid(row=2, column=1, padx=4)
        ttk.Label(p, text="DPI").grid(row=3, column=0, sticky="w")
        self.ent_dpi = ttk.Entry(p, width=8, textvariable=self.dpi); self.ent_dpi.grid(row=3, column=1, padx=4)
        ttk.Button(panel, text="Aplicar tamaño de hoja", command=self.apply_size).pack(fill=tk.X, pady=(6, 12))

        # --- Capas ---
        ttk.Label(panel, text="Capas (arriba = frente):").pack(anchor="w")
        self.layer_list = tk.Listbox(panel, height=8, exportselection=False)
        self.layer_list.pack(fill=tk.BOTH, expand=False)
        self.layer_list.bind("<<ListboxSelect>>", self.on_layer_select)
        b = ttk.Frame(panel); b.pack(fill=tk.X, pady=4)
        ttk.Button(b, text="Frente", command=self.bring_to_front).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(b, text="Fondo", command=self.send_to_back).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(b, text="Bloq/Desb", command=self.toggle_lock).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(panel, text="Eliminar capa", command=self.delete_selected).pack(fill=tk.X, pady=4)

        # --- Imagen seleccionada ---
        ttk.Label(panel, text="Imagen seleccionada").pack(anchor="w", pady=(10, 0))
        self.lbl_info = ttk.Label(panel, text="(sin selección)")
        self.lbl_info.pack(anchor="w")

        dims = ttk.Frame(panel); dims.pack(fill=tk.X, pady=(6,0))
        ttk.Label(dims, text="Ancho (cm)").grid(row=0, column=0, sticky="w")
        ttk.Label(dims, text="Alto (cm)").grid(row=1, column=0, sticky="w")
        self.img_w_cm = tk.StringVar(value="10.00")
        self.img_h_cm = tk.StringVar(value="10.00")
        self.ent_img_w = ttk.Entry(dims, width=8, textvariable=self.img_w_cm); self.ent_img_w.grid(row=0, column=1, padx=4)
        self.ent_img_h = ttk.Entry(dims, width=8, textvariable=self.img_h_cm); self.ent_img_h.grid(row=1, column=1, padx=4)
        self.keep_ratio = tk.BooleanVar(value=True)
        ttk.Checkbutton(dims, text="Mantener proporción", variable=self.keep_ratio).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4,0))
        ttk.Button(panel, text="Aplicar tamaño a imagen", command=self.apply_selected_size_cm).pack(fill=tk.X, pady=(6,6))

        # Escala
        ttk.Label(panel, text="Escala (%)").pack(anchor="w")
        self.scale_var = tk.DoubleVar(value=100.0)
        sc = ttk.Scale(panel, from_=5, to=1000, variable=self.scale_var, command=self.on_scale_slider)
        sc.pack(fill=tk.X)

        # Rotación
        ttk.Label(panel, text="Rotación (°)").pack(anchor="w", pady=(10,0))
        self.rot_var = tk.DoubleVar(value=0.0)
        r = ttk.Scale(panel, from_=-180, to=180, variable=self.rot_var, command=self.on_rot_slider); r.pack(fill=tk.X)
        rb = ttk.Frame(panel); rb.pack(fill=tk.X, pady=(4,0))
        ttk.Button(rb, text="Rot–", command=lambda: self.nudge_rotation(-5)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(rb, text="Rot+", command=lambda: self.nudge_rotation(+5)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # trazas para mantener proporción durante la edición
        self._suppress_traces = False
        self.img_w_cm.trace_add("write", self._on_w_edit)
        self.img_h_cm.trace_add("write", self._on_h_edit)

        ttk.Label(panel, text="Rueda: escala capa | Shift+rueda: rotar | Ctrl+rueda: zoom de vista").pack(anchor="w", pady=(8,0))
        return panel

    # ---------- Acciones ----------
    def apply_preset(self, _=None):
        m = self.preset.get()
        presets = {
            "33×48": (33.0, 48.0),
            "35×45": (35.0, 45.0),
            "30×50": (30.0, 50.0),
            "A3 (29.7×42)": (29.7, 42.0),
            "A4 (21×29.7)": (21.0, 29.7),
        }
        if m in presets:
            self.width_cm.set(presets[m][0]); self.height_cm.set(presets[m][1])
            self.apply_size(); self.fit_to_window()

    def new_canvas(self):
        if self.images and not messagebox.askyesno("Nuevo lienzo", "Esto limpiará las capas actuales. ¿Continuar?"):
            return
        self.images.clear(); self.selected = None
        self.update_layer_list(); self.apply_size(); self.fit_to_window()

    def menu_add_images(self):
        paths = filedialog.askopenfilenames(
            title="Selecciona imágenes",
            filetypes=[("Imágenes", "*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tiff;*.gif"), ("Todos", "*.*")]
        )
        if not paths: return
        self.add_images(paths)

    def menu_export_png(self):
        self.export_png()

    # ---------- Núcleo ----------
    def apply_size(self):
        try:
            dpi = int(self.dpi.get())
            w_cm = float(self.width_cm.get()); h_cm = float(self.height_cm.get())
            if dpi <= 0 or w_cm <= 0 or h_cm <= 0: raise ValueError
        except Exception:
            messagebox.showerror("Error", "Verifica que Ancho/Alto (cm) y DPI sean válidos (>0)."); return
        w_px = cm_to_px(w_cm, dpi); h_px = cm_to_px(h_cm, dpi)
        self.canvas_px = (w_px, h_px)
        for obj in self.images: obj.clear_cache()
        self._checker_tk = None
        self.update_scrollregion(); self.redraw_all()

    def update_scrollregion(self):
        w_v = int(self.canvas_px[0] * self.view_scale)
        h_v = int(self.canvas_px[1] * self.view_scale)
        self.canvas.config(scrollregion=(0, 0, w_v, h_v))
        self.ruler_top.config(scrollregion=(0, 0, w_v, 24))
        self.ruler_left.config(scrollregion=(0, 0, 28, h_v))

    def add_images(self, paths):
        w_px, h_px = self.canvas_px
        for p in paths:
            try:
                img = Image.open(p).convert("RGBA"); img.filename = p
            except Exception as e:
                messagebox.showerror("Error", f"No se pudo abrir la imagen:\n{p}\n{e}"); continue
            # Escala inicial para que quepa
            scale = 1.0
            max_side = max(img.width, img.height)
            if max_side > min(w_px, h_px) * 0.8:
                scale = (min(w_px, h_px) * 0.8) / max_side
            obj = LayerImage(img,
                             x=(w_px - int(img.width * scale)) // 2,
                             y=(h_px - int(img.height * scale)) // 2,
                             scale=scale, angle=0.0, opacity=1.0)
            obj.name = os.path.basename(p)
            self.images.append(obj); self.selected = obj
        self.update_layer_list(); self.sync_controls_from_selection(); self.redraw_all()

    def export_png(self):
        if self.canvas_px == (0, 0):
            messagebox.showerror("Error", "El lienzo no tiene tamaño válido."); return
        dpi = int(self.dpi.get()); w_px, h_px = self.canvas_px
        if not self.images and not messagebox.askyesno("Exportar vacío", "No hay imágenes. ¿Exportar PNG transparente?"):
            return
        out = Image.new("RGBA", (w_px, h_px), (0, 0, 0, 0))
        for obj in self.images:
            pil, _ = obj.render_cached(obj.scale)  # sin zoom de vista
            out.alpha_composite(pil, dest=(obj.x, obj.y))
        if self.mirror_on_export.get():
            out = ImageOps.mirror(out)  # espejo horizontal para DTF
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")],
                                            title="Guardar como PNG (transparente)")
        if not path: return
        try:
            out.save(path, format="PNG", dpi=(dpi, dpi))
            messagebox.showinfo("Exportado",
                                f"PNG exportado:\n{path}\n"
                                f"Tamaño: {w_px}×{h_px}px • DPI: {dpi}\n"
                                f"Dimensión física: {self.width_cm.get():.2f}×{self.height_cm.get():.2f} cm\n"
                                f"Espejo: {'Sí' if self.mirror_on_export.get() else 'No'}")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo exportar:\n{e}")

    # ---------- Dibujo ----------
    def sx(self, x): return int(round(x * self.view_scale))
    def sy(self, y): return int(round(y * self.view_scale))

    def redraw_all(self, fast=False):
        # Borrar y reiniciar ids para recrearlos
        self.canvas.delete("all")
        self.selection_rect = None
        for obj in self.images:
            obj.canvas_id = None

        if self.show_checker.get(): self.draw_checkerboard()
        if self.show_grid.get() and not (fast and self.fast_drag.get()): self.draw_cm_grid()
        for obj in self.images: self.draw_layer(obj)
        if self.selected: self.draw_selection(self.selected)
        if self.rulers_on.get() and not (fast and self.fast_drag.get()): self.draw_rulers()

    def make_checker(self, w, h, tile):
        img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        c1 = (238, 238, 238, 255); c2 = (255, 255, 255, 255)
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                img.paste(c1 if ((x//tile + y//tile) % 2 == 0) else c2,
                          (x, y, min(x+tile, w), min(y+tile, h)))
        return ImageTk.PhotoImage(img)

    def draw_checkerboard(self, base_tile=20):
        w = self.sx(self.canvas_px[0]); h = self.sy(self.canvas_px[1])
        tile = max(4, int(base_tile * self.view_scale))
        if not self._checker_tk or self._checker_tk.width() != w or self._checker_tk.height() != h:
            self._checker_tk = self.make_checker(max(1, w), max(1, h), tile)
        self.canvas.create_image(0, 0, image=self._checker_tk, anchor="nw")

    def draw_cm_grid(self):
        dpi = int(self.dpi.get())
        step_px = cm_to_px(self.GRID_CM, dpi)
        if self.view_scale < 0.4: step_px *= 5
        elif self.view_scale < 0.8: step_px *= 2
        w, h = self.canvas_px
        W, H = self.sx(w), self.sy(h)
        step_v = max(1, int(step_px * self.view_scale))
        for x in range(0, W+1, step_v):
            self.canvas.create_line(x, 0, x, H, fill="#bbbbbb")
        for y in range(0, H+1, step_v):
            self.canvas.create_line(0, y, W, y, fill="#bbbbbb")

    def draw_layer(self, obj: LayerImage):
        disp_scale = obj.scale * self.view_scale
        pil, tkimg = obj.render_cached(disp_scale)
        X, Y = self.sx(obj.x), self.sy(obj.y)
        if obj.canvas_id is None:
            obj.canvas_id = self.canvas.create_image(X, Y, image=tkimg, anchor="nw")
        else:
            self.canvas.itemconfig(obj.canvas_id, image=tkimg)
            self.canvas.coords(obj.canvas_id, X, Y)
        obj._last_tk = tkimg  # evitar GC

    def draw_selection(self, obj: LayerImage):
        disp_scale = obj.scale * self.view_scale
        pil, _ = obj.render_cached(disp_scale)
        x, y = self.sx(obj.x), self.sy(obj.y); w, h = pil.size
        pad = 3
        if self.selection_rect: self.canvas.delete(self.selection_rect)
        dash = () if obj.locked else (4, 2)
        self.selection_rect = self.canvas.create_rectangle(
            x - pad, y - pad, x + w + pad, y + h + pad,
            outline="#0078d4", width=2, dash=dash
        )

    def draw_rulers(self):
        self.ruler_top.delete("all"); self.ruler_left.delete("all")
        dpi = int(self.dpi.get()); w_px, h_px = self.canvas_px
        step = cm_to_px(1.0, dpi)
        W = self.sx(w_px); H = self.sy(h_px)
        self.ruler_top.create_rectangle(0, 0, W, 24, fill="#f4f4f4", outline="")
        self.ruler_left.create_rectangle(0, 0, 28, H, fill="#f4f4f4", outline="")
        step_view = max(40, int(step * self.view_scale))
        x = 0; cm_val = 0
        while x <= W:
            self.ruler_top.create_line(x, 24, x, 0, fill="#888")
            self.ruler_top.create_text(x+3, 12, anchor="w", text=str(cm_val), fill="#555")
            x += step_view; cm_val += 1
        y = 0; cm_val = 0
        while y <= H:
            self.ruler_left.create_line(28, y, 0, y, fill="#888")
            self.ruler_left.create_text(2, y+2, anchor="nw", text=str(cm_val), fill="#555")
            y += step_view; cm_val += 1

    # ---------- Eventos ----------
    def world_pos(self, sx, sy):
        return (self.canvas.canvasx(sx) / self.view_scale,
                self.canvas.canvasy(sy) / self.view_scale)

    def pick_object(self, x, y):
        for obj in reversed(self.images):
            x1, y1, x2, y2 = obj.bounds()
            if x1 <= x <= x2 and y1 <= y <= y2: return obj
        return None

    def on_click(self, event):
        wx, wy = self.world_pos(event.x, event.y)
        obj = self.pick_object(wx, wy)
        if obj:
            self.selected = obj
            self.drag_offset = (wx - obj.x, wy - obj.y)
            self.sync_controls_from_selection(); self.update_layer_list(select_selected=True)
        else:
            self.selected = None; self.update_layer_list()
        self.redraw_all()

    def on_drag(self, event):
        if self._panning: return self.do_pan_with_left(event)
        if not self.selected or self.selected.locked: return
        wx, wy = self.world_pos(event.x, event.y)
        ox, oy = self.drag_offset
        new_x, new_y = int(wx - ox), int(wy - oy)
        if self.snap_to_grid.get():
            step = cm_to_px(self.SNAP_CM, int(self.dpi.get()))
            new_x = int(round(new_x / step) * step); new_y = int(round(new_y / step) * step)
        self.selected.x, self.selected.y = new_x, new_y
        self.update_status(); self.redraw_all(fast=True)

    def on_release(self, event):
        if self.fast_drag.get(): self.redraw_all(fast=False)

    def on_right_click(self, event):
        wx, wy = self.world_pos(event.x, event.y)
        obj = self.pick_object(wx, wy)
        if obj:
            self.selected = obj; self.sync_controls_from_selection()
            self.update_layer_list(select_selected=True); self.redraw_all()
            self.ctx_menu.tk_popup(event.x_root, event.y_root)

    # <- ESTE método faltaba en tu archivo
    def on_motion(self, event):
        wx, wy = self.world_pos(event.x, event.y)
        dpi = int(self.dpi.get())
        self.status.config(text=f"Zoom: {self.view_scale*100:.0f}% • Cursor: {px_to_cm(wx, dpi):.2f}cm, {px_to_cm(wy, dpi):.2f}cm")

    # --- Zoom de vista (Ctrl+rueda) ---
    def on_mousewheel(self, event):
        ctrl = (event.state & 0x0004) != 0
        if not ctrl: return
        steps = 1 if getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4 else -1
        factor = 1.12 if steps > 0 else 1/1.12
        self.zoom_view(anchor=(event.x, event.y), factor=factor)

    def zoom_view(self, step=None, anchor=None, factor=None):
        if factor is None: factor = 1.12 if (step and step > 0) else 1/1.12
        old = self.view_scale; new = max(self.min_view, min(self.max_view, old * factor))
        if abs(new - old) < 1e-6: return
        if anchor is None: anchor = (self.canvas.winfo_width()//2, self.canvas.winfo_height()//2)
        ax, ay = anchor
        wx_before, wy_before = self.world_pos(ax, ay)
        self.view_scale = new
        self.update_scrollregion(); self.redraw_all()
        w_tot = max(1, self.sx(self.canvas_px[0])); h_tot = max(1, self.sy(self.canvas_px[1]))
        self.canvas.xview_moveto(max(0, min((wx_before*self.view_scale - ax)/w_tot, 1)))
        self.canvas.yview_moveto(max(0, min((wy_before*self.view_scale - ay)/h_tot, 1)))
        self._checker_tk = None

    def set_view_scale(self, s):
        self.view_scale = max(self.min_view, min(self.max_view, float(s)))
        self.update_scrollregion(); self.redraw_all(); self._checker_tk = None

    def fit_to_window(self):
        w, h = self.canvas_px
        if not w or not h: return
        vis_w = max(1, self.canvas.winfo_width()); vis_h = max(1, self.canvas.winfo_height())
        self.set_view_scale(min(vis_w / w, vis_h / h) * 0.98)
        self.canvas.xview_moveto(0); self.canvas.yview_moveto(0)

    # --- Paneo ---
    def start_pan(self, event): self._panning, self._pan_start = True, (event.x, event.y)
    def do_pan(self, event):
        if not self._panning: return
        dx, dy = event.x - self._pan_start[0], event.y - self._pan_start[1]
        self._pan_start = (event.x, event.y)
        self.canvas.xview_scroll(int(-dx), "units"); self.canvas.yview_scroll(int(-dy), "units")
    def stop_pan(self, event): self._panning = False
    def do_pan_with_left(self, event): pass

    # --- Rueda para capa seleccionada (Shift=rotar) ---
    def on_wheel_selected(self, event):
        if (getattr(event, "state", 0) & 0x0004) != 0: return  # Ctrl = zoom de vista
        if not self.selected or self.selected.locked: return
        steps = 1 if getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4 else -1
        if getattr(event, "state", 0) & 0x0001:  # Shift
            self.selected.angle = max(-180, min(180, self.selected.angle + (5 * steps)))
            self.selected.clear_cache()
            self.rot_var.set(self.selected.angle)
        else:
            factor = 1.05 if steps > 0 else 1/1.05
            self.selected.scale = max(0.02, min(20.0, self.selected.scale * factor))
            self.selected.clear_cache()
            self.scale_var.set(self.selected.scale * 100.0)
        self.update_status(); self.redraw_all(fast=True)

    # ---------- Controles de imagen ----------
    def on_scale_slider(self, _=None):
        if not self.selected: return
        self.selected.scale = float(self.scale_var.get()) / 100.0
        self.selected.clear_cache()
        self.redraw_all(); self.update_status(); self.update_selected_size_labels()

    def on_rot_slider(self, _=None):
        if not self.selected: return
        self.selected.angle = float(self.rot_var.get())
        self.selected.clear_cache()
        self.redraw_all(); self.update_status()

    def nudge_rotation(self, delta):
        if not self.selected or self.selected.locked: return
        self.selected.angle = max(-180, min(180, self.selected.angle + delta))
        self.selected.clear_cache()
        self.rot_var.set(self.selected.angle)
        self.redraw_all(); self.update_status()

    def _get_ratio(self):
        if not self.selected: return None
        ow, oh = self.selected.pil_original.size
        return oh / ow if ow else None

    def _on_w_edit(self, *args):
        if not self.keep_ratio.get() or not self.selected or getattr(self, "_suppress_traces", False): return
        ratio = self._get_ratio()
        try: w = float(self.img_w_cm.get())
        except Exception: return
        if ratio and w > 0:
            self._suppress_traces = True
            self.img_h_cm.set(f"{w*ratio:.2f}")
            self._suppress_traces = False

    def _on_h_edit(self, *args):
        if not self.keep_ratio.get() or not self.selected or getattr(self, "_suppress_traces", False): return
        ratio = self._get_ratio()
        try: h = float(self.img_h_cm.get())
        except Exception: return
        if ratio and h > 0:
            self._suppress_traces = True
            self.img_w_cm.set(f"{h/ratio:.2f}")
            self._suppress_traces = False

    def apply_selected_size_cm(self):
        if not self.selected: return
        dpi = int(self.dpi.get())
        try:
            target_w_cm = float(self.img_w_cm.get())
            target_h_cm = float(self.img_h_cm.get())
        except Exception:
            messagebox.showerror("Error", "Ingresa valores válidos de ancho/alto en cm."); return
        ow, oh = self.selected.pil_original.size
        desired_w_px = cm_to_px(max(0.01, target_w_cm), dpi)
        scale = desired_w_px / max(1, ow)
        if self.keep_ratio.get():
            new_h_cm = px_to_cm(int(round(oh * scale)), dpi)
            self._suppress_traces = True
            self.img_h_cm.set(f"{new_h_cm:.2f}")
            self._suppress_traces = False
        else:
            # si no mantiene proporción y dieron alto, adaptar usando el más restrictivo
            desired_h_px = cm_to_px(max(0.01, target_h_cm), dpi)
            scale = min(scale, desired_h_px / max(1, oh))
        self.selected.scale = max(0.02, min(20.0, scale))
        self.selected.clear_cache()
        self.scale_var.set(self.selected.scale * 100.0)
        self.redraw_all(); self.update_status(); self.update_selected_size_labels()

    def bump_scale(self, factor):
        if not self.selected or self.selected.locked: return
        self.selected.scale = max(0.02, min(20.0, self.selected.scale * factor))
        self.selected.clear_cache()
        self.scale_var.set(self.selected.scale * 100.0)
        self.redraw_all(); self.update_status(); self.update_selected_size_labels()

    def update_selected_size_labels(self):
        if not self.selected:
            self.lbl_info.config(text="(sin selección)")
            return
        dpi = int(self.dpi.get())
        ow, oh = self.selected.pil_original.size
        w_px = int(round(ow * self.selected.scale))
        h_px = int(round(oh * self.selected.scale))
        w_cm = px_to_cm(w_px, dpi); h_cm = px_to_cm(h_px, dpi)
        ppi = dpi / max(1e-6, self.selected.scale)
        self.lbl_info.config(text=f"Original: {ow}×{oh}px | Impreso: {w_cm:.2f}×{h_cm:.2f} cm | ~{ppi:.0f} PPI")
        self._suppress_traces = True
        self.img_w_cm.set(f"{w_cm:.2f}"); self.img_h_cm.set(f"{h_cm:.2f}")
        self._suppress_traces = False

    # ---------- Capas ----------
    def update_layer_list(self, select_selected=False):
        self.layer_list.delete(0, tk.END)
        for i, obj in enumerate(self.images):
            tag = "🔒 " if obj.locked else ""
            self.layer_list.insert(tk.END, f"{i:02d} • {tag}{obj.name}")
        if select_selected and self.selected in self.images:
            idx = self.images.index(self.selected)
            self.layer_list.selection_clear(0, tk.END)
            self.layer_list.selection_set(idx); self.layer_list.see(idx)

    def on_layer_select(self, _):
        sel = self.layer_list.curselection()
        if not sel: return
        idx = sel[0]
        if 0 <= idx < len(self.images):
            self.selected = self.images[idx]
            self.sync_controls_from_selection(); self.redraw_all()

    def bring_to_front(self):
        if not self.selected: return
        if self.selected in self.images:
            self.images.remove(self.selected); self.images.append(self.selected)
            self.update_layer_list(select_selected=True); self.redraw_all()

    def send_to_back(self):
        if not self.selected: return
        if self.selected in self.images:
            self.images.remove(self.selected); self.images.insert(0, self.selected)
            self.update_layer_list(select_selected=True); self.redraw_all()

    def toggle_lock(self):
        if not self.selected: return
        self.selected.locked = not self.selected.locked
        self.update_layer_list(select_selected=True); self.redraw_all()

    def delete_selected(self, event=None):
        if not self.selected: return
        if self.selected.canvas_id: self.canvas.delete(self.selected.canvas_id)
        try: self.images.remove(self.selected)
        except ValueError: pass
        self.selected = None; self.update_layer_list(); self.redraw_all()
        self.status.config(text="Capa eliminada")

    def sync_controls_from_selection(self):
        if not self.selected:
            self.scale_var.set(100.0); self.rot_var.set(0.0); self.lbl_info.config(text="(sin selección)")
        else:
            self.scale_var.set(self.selected.scale * 100.0)
            self.rot_var.set(self.selected.angle)
            self.update_selected_size_labels()
        self.update_status()

    def update_status(self):
        if not self.selected:
            self.status.config(text=f"Zoom: {self.view_scale*100:.0f}%"); return
        dpi = int(self.dpi.get())
        x_cm = px_to_cm(self.selected.x, dpi); y_cm = px_to_cm(self.selected.y, dpi)
        self.status.config(text=(f"Zoom: {self.view_scale*100:.0f}% • "
                                 f"Sel: {self.selected.name} • Pos: {x_cm:.2f}cm,{y_cm:.2f}cm • "
                                 f"Escala: {self.selected.scale:.2f} • Rot: {self.selected.angle:.1f}°"))

def main():
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "clam" in style.theme_names(): style.theme_use("clam")
    except Exception: pass
    app = TransparentCanvasApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()



"""Small Windows background window; tkinter is imported only when requested."""

import webbrowser


def desktop_window(server, url):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("KillZHS")
    window.geometry("390x220")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="KillZHS 正在运行", font=("Microsoft YaHei UI", 16)).pack(anchor="w")
    ttk.Label(frame, text="在浏览器中管理课程、AI 和定时计划。").pack(anchor="w", pady=(10, 4))
    status = ttk.Label(frame, text="关闭网页后，后台计划仍可运行。")
    status.pack(anchor="w", pady=(0, 16))
    ttk.Button(frame, text="打开工作台", command=lambda: webbrowser.open(url)).pack(side="left")

    def quit_app():
        server.closing.set()
        window.destroy()

    ttk.Button(frame, text="退出并停止任务", command=quit_app).pack(side="right")
    window.protocol("WM_DELETE_WINDOW", quit_app)

    def tick():
        if server.closing.is_set():
            window.destroy()
            return
        with server.controller.lock:
            status.configure(text=server.controller.message[:35])
        window.after(1000, tick)

    window.after(1000, tick)
    window.after(500, window.iconify)
    window.mainloop()

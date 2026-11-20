# ==============================================================================
# START OF BLOCK 1: IMPORTS AND CORE LOGIC ENGINE
# ==============================================================================
import os
import subprocess
import shutil
import fnmatch  
import hou
from PySide6 import QtWidgets, QtCore, QtGui

class RatManagerCore:
    """Handles scene scraping, disk checking, and file operations."""
    
    IMAGE_PARM_NAMES = [
        "map", "file", "filename", "texture", "tex", "basecolor_texture", 
        "rough_texture", "metallic_texture", "normal_texture", "bump_texture",
        "picture", "vm_picture", "output", "renderoutput"
    ]

    RENDER_OUTPUT_NODE_TYPES = [
        "ifd", "karma", "usdrender", "usdrenderproperties", "arnold", "vray", 
        "geometry", "comp", "render", "channel", "baketexture", "labs::maps_baker"
    ]

    @classmethod
    def is_render_output_node(cls, node_type_name):
        """Checks if a node type falls under render settings or file cache outputs."""
        name_lower = node_type_name.lower()
        return any(t in name_lower for t in cls.RENDER_OUTPUT_NODE_TYPES)

    @classmethod
    def find_texture_nodes(cls):
        """Scrapes the Houdini scene for nodes containing texture parameters."""
        texture_data = []
        for node in hou.node("/").allNodes():
            if node.isInsideLockedHDA():
                continue
            node_type = node.type().name()
            is_render_node = cls.is_render_output_node(node_type)
            
            for parm in node.parms():
                if parm.name() in cls.IMAGE_PARM_NAMES or isinstance(parm.parmTemplate(), hou.StringParmTemplate):
                    val = parm.eval()
                    raw = parm.rawValue()
                    if val and any(val.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.tga', '.exr', '.tif', '.tiff', '.rat']):
                        
                        # CHANGED: Adjusted to handle .rat by appending it directly to the path instead of replacing extensions
                        if raw.lower().endswith('.rat'):
                            rat_raw = raw
                            if raw.lower().endswith('.exr.rat'):
                                orig_raw = raw[:-4]  # Safely strip off the trailing '.rat' to restore the full file path
                            else:
                                base_raw, _ = os.path.splitext(raw[:-4])
                                orig_raw = base_raw + ".exr"  # Fallback recovery strategy if path format looks unconventional
                        else:
                            orig_raw = raw
                            rat_raw = f"{raw}.rat"

                        texture_data.append({
                            "node_path": node.path(),
                            "node_type": node_type,
                            "is_render_node": is_render_node,
                            "parm_name": parm.name(),
                            "raw_path": raw,
                            "orig_raw": orig_raw,
                            "rat_raw": rat_raw,
                            "eval_path": val
                        })
        return texture_data

    @classmethod
    def get_rat_path(cls, orig_path):
        """Returns the intended evaluation path for the .rat file on disk (appending instead of replacing)."""
        # CHANGED: Appends .rat onto the existing path instead of using os.path.splitext()
        return f"{orig_path}.rat"

    @classmethod
    def verify_status(cls, orig_path):
        """Checks disk availability for both the original texture and its .rat pair."""
        if not orig_path:
            return "Missing Source", False, False
        
        # CHANGED: Corrected verification logic to match the appended file naming style
        if orig_path.lower().endswith('.rat'):
            src_path = orig_path[:-4]  # Strip trailing '.rat' to locate source file position
            src_exists = os.path.exists(src_path)
            rat_exists = os.path.exists(orig_path)
        else:
            src_exists = os.path.exists(orig_path)
            rat_path = cls.get_rat_path(orig_path)
            rat_exists = os.path.exists(rat_path)

        if not src_exists and not rat_exists:
            return "Missing All", False, False
        if src_exists and not rat_exists:
            return "No RAT", True, False
        if not src_exists and rat_exists:
            return "RAT Only", False, True
        return "Optimized", True, True
# ==============================================================================
# END OF BLOCK 1: IMPORTS AND CORE LOGIC ENGINE
# ==============================================================================


# ==============================================================================
# START OF BLOCK 2: ASYNCHRONOUS BACKGROUND WORKER
# ==============================================================================
class ConvertWorker(QtCore.QThread):
    """Asynchronously generates RAT files using Houdini's iconvert utility."""
    progress_signal = QtCore.Signal(str, int)
    finished_signal = QtCore.Signal()

    def __init__(self, conversion_tasks, parent=None):
        super().__init__(parent)
        self.tasks = conversion_tasks # List of (src, dest)

    def run(self):
        total = len(self.tasks)
        for idx, (src, dest) in enumerate(self.tasks):
            if not os.path.exists(src):
                self.progress_signal.emit(f"Failed: Source missing {os.path.basename(src)}", idx + 1)
                continue
            
            self.progress_signal.emit(f"Converting: {os.path.basename(src)}", idx + 1)
            
            iconvert_bin = "iconvert"
            if os.name == 'nt':
                iconvert_bin = os.path.join(os.environ.get("HFS", ""), "bin", "iconvert.exe")
                
            try:
                subprocess.run([iconvert_bin, src, dest], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception as e:
                print(f"Error converting {src}: {e}")
                
        self.finished_signal.emit()
# ==============================================================================
# END OF BLOCK 2: ASYNCHRONOUS BACKGROUND WORKER
# ==============================================================================

# ==============================================================================
# START OF BLOCK 3: UI SETUP AND COMPONENT LAYOUT
# ==============================================================================
class RatManagerWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=hou.qt.mainWindow()):
        super().__init__(parent)
        self.setWindowTitle("RAT File Manager (Houdini 22)")
        self.resize(1100, 650)
        
        self.all_items_data = []
        self.setup_ui()
        self.refresh_data()

    def setup_ui(self):
        main_widget = QtWidgets.QWidget()
        self.setCentralWidget(main_widget)
        layout = QtWidgets.QVBoxLayout(main_widget)

        # --- Filter & Search Toolbar ---
        filter_bar = QtWidgets.QHBoxLayout()
        
        self.search_bar = QtWidgets.QLineEdit()
        self.search_bar.setPlaceholderText("Search by node path or texture filename...")
        self.search_bar.textChanged.connect(self.filter_tree)
        filter_bar.addWidget(self.search_bar)

        self.status_filter = QtWidgets.QComboBox()
        self.status_filter.addItems(["All Statuses", "Optimized (.rat)", "No RAT", "Missing Source", "RAT Only"])
        self.status_filter.currentIndexChanged.connect(self.filter_tree)
        filter_bar.addWidget(self.status_filter)

        self.network_filter = QtWidgets.QComboBox()
        self.network_filter.addItems(["All Networks", "/obj", "/mat", "/stage", "/img"])
        self.network_filter.currentIndexChanged.connect(self.filter_tree)
        filter_bar.addWidget(self.network_filter)

        # New: Toggle to filter out ROP / Render Output paths 
        self.exclude_render_cb = QtWidgets.QCheckBox("Exclude Render/Output Nodes")
        self.exclude_render_cb.setChecked(True)
        self.exclude_render_cb.stateChanged.connect(self.filter_tree)
        filter_bar.addWidget(self.exclude_render_cb)

        layout.addLayout(filter_bar)

        # --- Tree Widget ---
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["Network / Node / Parameter", "Status", "Source File", "RAT File", "Node Type", "Raw String"])
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.tree)

        # --- Action Buttons ---
        btn_layout = QtWidgets.QHBoxLayout()
        
        btn_refresh = QtWidgets.QPushButton("Refresh Scene Tree")
        btn_refresh.clicked.connect(self.refresh_data)
        btn_layout.addWidget(btn_refresh)

        btn_gen = QtWidgets.QPushButton("Generate Selected RATs")
        btn_gen.setStyleSheet("background-color: #3a6a3a; font-weight: bold;")
        btn_gen.clicked.connect(self.generate_rats)
        btn_layout.addWidget(btn_gen)

        btn_del = QtWidgets.QPushButton("Delete Selected RATs")
        btn_del.setStyleSheet("background-color: #7a3a3a;")
        btn_del.clicked.connect(self.delete_rats)
        btn_layout.addWidget(btn_del)

        layout.addLayout(btn_layout)

        # --- Progress Bar ---
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
# ==============================================================================
# END OF BLOCK 3: UI SETUP AND COMPONENT LAYOUT
# ==============================================================================
# ==============================================================================
# START OF BLOCK 4: TREE POPULATION AND FILTERING LOGICS
# ==============================================================================
    def refresh_data(self):
        """Scrapes Houdini and rebuilds the hierarchical tree layout."""
        self.tree.clear()
        self.all_items_data = RatManagerCore.find_texture_nodes()
        root_nodes = {}

        for data in self.all_items_data:
            node_path = data["node_path"]
            status, src_ok, rat_ok = RatManagerCore.verify_status(data["eval_path"])
            data["status"] = status
            
            # CHANGED: Adjusted evaluation track to correctly recognize appended naming formats
            if data["eval_path"].lower().endswith('.rat'):
                data["rat_eval_path"] = data["eval_path"]
                data["eval_path"] = data["eval_path"][:-4]  # Display source file context natively in the tree row
            else:
                data["rat_eval_path"] = RatManagerCore.get_rat_path(data["eval_path"])

            parts = node_path.strip("/").split("/")
            parent_item = self.tree.invisibleRootItem()
            
            current_path = ""
            for i, part in enumerate(parts):
                current_path += f"/{part}"
                if current_path not in root_nodes:
                    new_item = QtWidgets.QTreeWidgetItem(parent_item, [part])
                    new_item.setData(0, QtCore.Qt.UserRole, {"type": "dir", "path": current_path})
                    if i == len(parts) - 1:
                        new_item.setText(4, data["node_type"])
                        try:
                            node_icon = hou.qt.Icon(data["node_type"], 16, 16) if hou.qt.Icon(data["node_type"]) else QtGui.QIcon()
                            new_item.setIcon(0, node_icon)
                        except (hou.OperationFailed, Exception):
                            new_item.setIcon(0, QtGui.QIcon())
                            
                    root_nodes[current_path] = new_item
                parent_item = root_nodes[current_path]

            parm_item = QtWidgets.QTreeWidgetItem(parent_item, [
                data["parm_name"], 
                status, 
                os.path.basename(data["eval_path"]), 
                os.path.basename(data["rat_eval_path"]),
                "",
                data["raw_path"]
            ])
            parm_item.setData(0, QtCore.Qt.UserRole, {"type": "parm", "data": data})
            
            if status == "Optimized":
                parm_item.setForeground(1, QtGui.QColor("#4CAF50"))
            elif status == "No RAT":
                parm_item.setForeground(1, QtGui.QColor("#FFC107"))
            else:
                parm_item.setForeground(1, QtGui.QColor("#F44336"))

        self.tree.expandAll()
        self.filter_tree()

    def filter_tree(self):
        """Hides/Shows items according to the filter controls, including wildcard patterns."""
        search_txt = self.search_bar.text().strip().lower()
        status_txt = self.status_filter.currentText()
        net_txt = self.network_filter.currentText()
        exclude_render = self.exclude_render_cb.isChecked()

        if search_txt and '*' not in search_txt:
            search_pattern = f"*{search_txt}*"
        else:
            search_pattern = search_txt

        def evaluate_visibility(item):
            is_parm = item.data(0, QtCore.Qt.UserRole) and item.data(0, QtCore.Qt.UserRole).get("type") == "parm"
            
            if is_parm:
                p_data = item.data(0, QtCore.Qt.UserRole)["data"]
                
                if exclude_render and p_data["is_render_node"]:
                    item.setHidden(True)
                    return False

                if search_pattern:
                    match_search = (
                        fnmatch.fnmatch(p_data["node_path"].lower(), search_pattern) or
                        fnmatch.fnmatch(p_data["eval_path"].lower(), search_pattern) or
                        fnmatch.fnmatch(p_data["parm_name"].lower(), search_pattern)
                    )
                else:
                    match_search = True

                match_status = (status_txt == "All Statuses" or p_data["status"] == status_txt)
                match_net = (net_txt == "All Networks" or p_data["node_path"].startswith(net_txt))

                visible = match_search and match_status and match_net
                item.setHidden(not visible)
                return visible
            else:
                any_child_visible = False
                for idx in range(item.childCount()):
                    if evaluate_visibility(item.child(idx)):
                        any_child_visible = True
                item.setHidden(not any_child_visible)
                return any_child_visible

        for idx in range(self.tree.topLevelItemCount()):
            evaluate_visibility(self.tree.topLevelItem(idx))

    def get_selected_parm_data(self):
        collected = []
        def gather(item):
            role_data = item.data(0, QtCore.Qt.UserRole)
            if role_data and role_data.get("type") == "parm":
                collected.append(role_data["data"])
            for i in range(item.childCount()):
                gather(item.child(i))

        for item in self.tree.selectedItems():
            gather(item)
        return collected
# ==============================================================================
# END OF BLOCK 4: TREE POPULATION AND FILTERING LOGICS
# ==============================================================================

# ==============================================================================
# START OF BLOCK 5: ACTIONS, CONTEXT MENUS, AND PANEL INTERFACE INTEGRATION
# ==============================================================================
    def show_context_menu(self, position):
        menu = QtWidgets.QMenu()
        act_select_node = menu.addAction("Select Matching Nodes in Houdini")
        menu.addSeparator()
        act_switch_rat = menu.addAction("Toggle Parameter to use .rat Path")
        act_switch_orig = menu.addAction("Toggle Parameter to use Original Path")
        menu.addSeparator()
        
        # Added Generate and Delete actions into the right-click context menu
        act_gen_rat = menu.addAction("Generate Selected RATs")
        act_del_rat = menu.addAction("Delete Selected RATs")
        
        action = menu.exec_(self.tree.viewport().mapToGlobal(position))
        selected_data = self.get_selected_parm_data()
        
        if not selected_data:
            return

        if action == act_select_node:
            hou.clearAllSelected()
            for d in selected_data:
                hou.node(d["node_path"]).setSelected(True, clear_all_selected=False)
                
        elif action in [act_switch_rat, act_switch_orig]:
            for d in selected_data:
                parm = hou.parm(f"{d['node_path']}/{d['parm_name']}")
                if parm:
                    if action == act_switch_rat:
                        parm.set(d["rat_raw"])
                    else:
                        parm.set(d["orig_raw"])
            self.refresh_data()
            
        elif action == act_gen_rat:
            self.generate_rats()
            
        elif action == act_del_rat:
            self.delete_rats()

    def generate_rats(self):
        selected_data = self.get_selected_parm_data()
        if not selected_data:
            QtWidgets.QMessageBox.warning(self, "No Selection", "Please highlight parameters or network folders to generate RAT textures.")
            return

        tasks = []
        overwrite_prompt_needed = False

        for d in selected_data:
            if not d["eval_path"] or d["eval_path"].lower().endswith('.rat'):
                continue
            rat_path = d["rat_eval_path"]
            if os.path.exists(rat_path):
                overwrite_prompt_needed = True
            tasks.append((d["eval_path"], rat_path))

        if not tasks:
            return

        if overwrite_prompt_needed:
            reply = QtWidgets.QMessageBox.question(
                self, 
                "Confirm Overwrite", 
                "One or more .rat destinations already exist on disk. Overwrite files?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(tasks))
        self.progress_bar.setValue(0)
        
        self.worker = ConvertWorker(tasks, self)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.conversion_complete)
        self.worker.start()

    def update_progress(self, msg, count):
        self.progress_bar.setValue(count)
        self.statusBar().showMessage(msg)

    def conversion_complete(self):
        self.progress_bar.setVisible(False)
        self.statusBar().showMessage("Conversion process completed successfully.", 5000)
        self.refresh_data()

    def delete_rats(self):
        selected_data = self.get_selected_parm_data()
        rat_paths_to_delete = [d["rat_eval_path"] for d in selected_data if d["rat_eval_path"] and os.path.exists(d["rat_eval_path"])]

        if not rat_paths_to_delete:
            QtWidgets.QMessageBox.information(self, "No Files Found", "No generated .rat files were found on disk matching selection.")
            return

        reply = QtWidgets.QMessageBox.question(
            self, 
            "Confirm Delete", 
            f"Are you sure you want to permanently delete {len(rat_paths_to_delete)} .rat files from disk?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        for p in rat_paths_to_delete:
            try:
                os.remove(p)
            except Exception as e:
                print(f"Error clearing {p}: {e}")

        self.refresh_data()

def onCreateInterface():
    """Houdini's required entry point for instantiating the Python Panel interface."""
    return RatManagerWindow()
# ==============================================================================
# END OF BLOCK 5: ACTIONS, CONTEXT MENUS, AND PANEL INTERFACE INTEGRATION
# ==============================================================================


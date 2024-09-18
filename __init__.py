import auto_editor
import bpy
import hashlib
import os
import shutil
import subprocess
import sys

from auto_editor.formats import json as ae_json
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator

# bl_info constants
BL_INFO_VERSION = (1, 0)
BL_INFO_BLENDER = (2, 90, 0)

bl_info = {
    "name": "Detect Shots and Split Strips",
    "author": "Tintwotin, Brandon Castellano(PySceneDetect-module)",
    "version": BL_INFO_VERSION,
    "blender": BL_INFO_BLENDER,
    "location": "Sequencer > Strip Menu or Context Menu",
    "description": "Detect shots in active strip and split all selected strips accordingly.",
    "warning": "",
    "doc_url": "",
    "category": "Sequencer",
}

# Centralize cache handling
AUTO_EDITOR_CACHE_DIR = f"{os.environ['XDG_CONFIG_HOME']}/auto-editor-cache"


# Preferences
class MyAddonPreferences(AddonPreferences):
    bl_idname = __name__

    split_type: EnumProperty(
        name="Default Split Type",
        description="Choose the default split type for shot detection",
        items=(
            ("SOFT", "Soft", "Split Soft"),
            ("HARD", "Hard", "Split Hard"),
        ),
        default="SOFT",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "split_type")


# Scene detection (import when needed)
def find_scenes(video_path, threshold, start, end):
    from scenedetect import ContentDetector, SceneManager, open_video

    render = bpy.context.scene.render
    fps = round((render.fps / render.fps_base), 3)
    video = open_video(video_path, framerate=fps)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    video.seek((start / fps))
    scene_manager.detect_scenes(video, end_time=(end / fps))

    return scene_manager.get_scene_list()


# Split selected strips
class SEQUENCER_OT_split_selected(bpy.types.Operator):
    """Split Unlocked Un/Seleted Strips Soft"""

    bl_idname = "sequencer.split_selected"
    bl_label = "Split Selected"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.sequences)

    def execute(self, context):
        selection = context.selected_sequences
        sequences = bpy.context.scene.sequence_editor.sequences_all
        cf = bpy.context.scene.frame_current
        at_cursor = []
        cut_selected = False

        # Get default split type from preferences
        user_preferences = context.preferences
        addon_prefs = user_preferences.addons[__name__].preferences
        split_type = addon_prefs.split_type

        # Find unlocked strips at cursor
        for s in sequences:
            if s.frame_final_start <= cf and s.frame_final_end > cf and not s.lock:
                at_cursor.append(s)
                cut_selected = cut_selected or s.select

        for s in at_cursor:
            if cut_selected and s.select:
                bpy.ops.sequencer.select_all(action="DESELECT")
                s.select = True
                bpy.ops.sequencer.split(
                    frame=cf,
                    type=split_type,
                    side="RIGHT",
                )

                # Add new strip to selection
                for i in bpy.context.scene.sequence_editor.sequences_all:
                    if i.select:
                        selection.append(i)
                bpy.ops.sequencer.select_all(action="DESELECT")
                for s in selection:
                    s.select = True

        return {"FINISHED"}


# Detect shots and split
class SEQUENCER_OT_detect_shots(Operator):
    """Detect shots in active strip and split all selected strips accordingly"""

    bl_idname = "sequencer.detect_shots"
    bl_label = "Detect Shots & Split Strips"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.scene
            and context.scene.sequence_editor
            and context.scene.sequence_editor.active_strip
            and context.scene.sequence_editor.active_strip.type == "MOVIE"
        )

    def execute(self, context):
        scene = context.scene
        sequencer = bpy.ops.sequencer
        cf = context.scene.frame_current
        path = context.scene.sequence_editor.active_strip.filepath
        path = os.path.realpath(bpy.path.abspath(path))

        self.report({"INFO"}, f"Please wait. Detecting shots in {path}.")

        active = context.scene.sequence_editor.active_strip
        start_time = active.frame_offset_start
        end_time = active.frame_duration - active.frame_offset_end
        scenes = find_scenes(path, 27, start_time, end_time)
        for scene in scenes:
            context.scene.frame_current = int(
                scene[1].get_frames() + active.frame_start
            )
            sequencer.split_selected()

        context.scene.frame_current = cf

        self.report({"INFO"}, "Finished: Shot detection and strip splitting.")
        return {"FINISHED"}


# Detect shots using Auto-Editor and split
class SEQUENCER_OT_detect_shots_auto_editor(Operator):
    """Detect shots using Auto-Editor and split all selected strips accordingly"""

    bl_idname = "sequencer.detect_shots_auto_editor"
    bl_label = "Detect Shots (Auto-Editor) & Split Strips"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.scene
            and context.scene.sequence_editor
            and context.scene.sequence_editor.active_strip
            and context.scene.sequence_editor.active_strip.type == "MOVIE"
        )

    def execute(self, context):
        scene = context.scene
        sequencer = bpy.ops.sequencer
        cf = context.scene.frame_current
        path = context.scene.sequence_editor.active_strip.filepath
        path = os.path.realpath(bpy.path.abspath(path))

        # Hash the video file path
        file_hash = hashlib.md5(path.encode()).hexdigest()

        # Construct cached JSON path
        cached_json_path = os.path.join(AUTO_EDITOR_CACHE_DIR, f"{file_hash}.json")

        active = context.scene.sequence_editor.active_strip

        if os.path.exists(cached_json_path):
            # Load cached JSON
            self.report({"INFO"}, f"Loading cached Auto-Editor results for {path}")
            timeline = ae_json.read_json(cached_json_path, auto_editor.utils.log.Log())
        else:
            # Run Auto-Editor
            self.report({"INFO"}, f"Detecting shots in {path} using Auto-Editor.")

            try:
                # Construct Auto-Editor command (customize as needed)
                command = [
                    "auto-editor",
                    path,
                    "--export_as_json",  # Ensure JSON output
                ]

                # Run Auto-Editor
                completed_process = subprocess.run(command, check=True)

                if completed_process.returncode != 0:
                    self.report(
                        {"ERROR"},
                        f"Auto-Editor exited with error code {completed_process.returncode}",
                    )
                    return {"CANCELLED"}

                original_json_path = os.path.splitext(path)[0] + "_ALTERED.json"

                # Cache the JSON output
                try:
                    os.makedirs(AUTO_EDITOR_CACHE_DIR, exist_ok=True)
                    shutil.copy2(original_json_path, cached_json_path)
                except (OSError, shutil.Error) as e:
                    self.report({"ERROR"}, f"Error caching Auto-Editor results: {e}")
                    return {"CANCELLED"}

                # Load JSON from the cached location
                try:
                    timeline = ae_json.read_json(
                        cached_json_path, auto_editor.utils.log.Log()
                    )
                except (FileNotFoundError, ae_json.JSONDecodeError) as e:
                    self.report({"ERROR"}, f"Error loading Auto-Editor results: {e}")
                    return {"CANCELLED"}

            except (subprocess.CalledProcessError, ImportError, AttributeError) as e:
                self.report({"ERROR"}, f"Error using Auto-Editor: {e}")
                return {"CANCELLED"}

        # Make cuts in the sequencer
        for video_clips in timeline.v:
            for clip in video_clips:
                if isinstance(clip, auto_editor.timeline.TlVideo):
                    context.scene.frame_current = int(clip.offset + active.frame_start)
                    sequencer.split_selected()

                    context.scene.frame_current = int(
                        clip.offset + clip.dur + active.frame_start
                    )
                    sequencer.split_selected()

        # Deselect every second strip
        selected_strips = [
            strip
            for strip in bpy.context.scene.sequence_editor.sequences
            if strip.select
        ]

        for i, strip in enumerate(selected_strips):
            if i % 4 == 0 or i % 4 == 1:
                strip.select = False

        context.scene.frame_current = cf

        self.report(
            {"INFO"}, "Finished: Shot detection and strip splitting using Auto-Editor."
        )
        return {"FINISHED"}


# Menu integration
def menu_detect_shots(self, context):
    self.layout.separator()
    self.layout.operator("sequencer.detect_shots")
    self.layout.operator("sequencer.detect_shots_auto_editor")


classes = (
    SEQUENCER_OT_detect_shots,
    SEQUENCER_OT_split_selected,
    SEQUENCER_OT_detect_shots_auto_editor,
    MyAddonPreferences,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.SEQUENCER_MT_context_menu.append(menu_detect_shots)
    bpy.types.SEQUENCER_MT_strip.append(menu_detect_shots)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.SEQUENCER_MT_context_menu.remove(menu_detect_shots)
    bpy.types.SEQUENCER_MT_strip.remove(menu_detect_shots)


if __name__ == "__main__":
    register()


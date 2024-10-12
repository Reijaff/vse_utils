import bpy
import auto_editor
import tempfile
import hashlib
import base64
import requests
import os
import shutil
import subprocess
from tqdm import tqdm

import warnings



import sys
import pysubs2

from auto_editor.formats import json as ae_json
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator

import check_swear

# Ignore all InconsistentVersionWarning warnings
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn") 

sch = check_swear.SwearingCheck(stop_words=["ахуенно", "поебень", "поебалу", "выпиздили" ])

bl_info = {
    "name": "vse utils",
    "author": "reijaff",
    "version": (1, 0),
    "blender": (2, 90, 0),
    "location": "Sequencer > Strip Menu or Context Menu",
    "description": "shot detection, audo edit, mute profanity, speechnorm filter",
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


class SEQUENCER_OT_split_selected(bpy.types.Operator):

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


class SEQUENCER_OT_detect_shots(Operator):

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


class SEQUENCER_OT_auto_editor_audio(Operator):

    bl_idname = "sequencer.auto_editor_audio"
    bl_label = "Auto-Editor (audio)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene and scene.sequence_editor and scene.sequence_editor.active_strip):
            return False

        if scene.sequence_editor.active_strip.type != "SOUND":
            return False

        first_channel = get_selected_strips()[0].channel
        return all(strip.channel == first_channel for strip in get_selected_strips())

    def execute(self, context):
        cf = context.scene.frame_current
        path = context.scene.sequence_editor.active_strip.sound.filepath
        path = os.path.realpath(bpy.path.abspath(path))

        # Hash the video file path
        file_hash = hashlib.md5(path.encode()).hexdigest()

        cached_json_path = os.path.join(AUTO_EDITOR_CACHE_DIR, f"{file_hash}.json")

        audio_start, audio_end, tmp_audiofile_path = create_temp_sound_mixdown(
            get_selected_strips()
        )

        # Construct Auto-Editor command (customize as needed)
        command = [
            "auto-editor",
            tmp_audiofile_path,
            "--export_as_json",
            "--frame-rate",
            str(bpy.context.scene.render.fps),
        ]

        # Run Auto-Editor
        completed_process = subprocess.run(command, check=True)

        if completed_process.returncode != 0:
            self.report(
                {"ERROR"},
                f"Auto-Editor exited with error code {completed_process.returncode}",
            )
            return {"CANCELLED"}

        original_json_path = os.path.splitext(tmp_audiofile_path)[0] + "_ALTERED.json"

        # Cache the JSON output
        os.makedirs(AUTO_EDITOR_CACHE_DIR, exist_ok=True)
        shutil.copy2(original_json_path, cached_json_path)

        # Load JSON from the cached location
        timeline = ae_json.read_json(cached_json_path, auto_editor.utils.log.Log())

        content_array = []

        # Make cuts in the sequencer
        for video_clips in timeline.a:
            for clip in video_clips:
                context.scene.frame_current = int(clip.offset + audio_start)
                bpy.ops.sequencer.select_all(action="SELECT")
                bpy.ops.sequencer.split(type="SOFT")

                context.scene.frame_current = int(clip.offset + clip.dur + audio_start)
                bpy.ops.sequencer.select_all(action="SELECT")
                bpy.ops.sequencer.split(type="SOFT")

                content_array.append(
                    [
                        int(clip.offset + audio_start),
                        int(clip.offset + clip.dur + audio_start),
                    ]
                )

        bpy.ops.sequencer.select_all(action="DESELECT")

        strips_in_range = [
            strip
            for strip in bpy.context.scene.sequence_editor.sequences
            if audio_start <= strip.frame_final_start <= audio_end
            and audio_start <= strip.frame_final_end <= audio_end
        ]

        for i, strip in enumerate(strips_in_range):
            for strc in content_array:
                if (
                    strip.frame_final_start >= strc[0]
                    and strip.frame_final_end <= strc[1]
                ):
                    strip.select = True

        context.scene.frame_current = cf

        self.report({"INFO"}, "Finished: strip splitting using Auto-Editor.")

        os.remove(tmp_audiofile_path)
        return {"FINISHED"}


def send_audio_for_transcription(audio_file_path, server_url):
    transcription_data = None

    with open(audio_file_path, "rb") as audio_file:
        audio_data = audio_file.read()
        audio_base64 = base64.b64encode(audio_data).decode("utf-8")

    srt_file_path = tempfile.NamedTemporaryFile(suffix=".srt", delete=False)

    data = {"audio_base64": audio_base64, "srt_file_path": srt_file_path.name}

    try:
        response = requests.post(server_url, json=data)
        response.raise_for_status()  # Raise an exception for bad status codes

        transcription_data = response.json()
        # print(transcription_data)
    except requests.exceptions.RequestException as e:
        print(f"Error sending audio for transcription: {e}")
    return (transcription_data, srt_file_path.name)


def get_selected_strips():
    selected_strips = []
    for strip in bpy.context.scene.sequence_editor.sequences:
        if strip.select == True:
            selected_strips.append(strip)
    return selected_strips


def create_temp_sound_mixdown(selected_strips):

    # Calculate the overall time range of the selected strips
    audio_start = min(strip.frame_final_start for strip in selected_strips)
    audio_end = max(strip.frame_final_end for strip in selected_strips)

    # Temporarily adjust the scene's time range to focus on the selected audio
    original_frame_start, original_frame_end = (
        bpy.context.scene.frame_start,
        bpy.context.scene.frame_end,
    )
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = audio_start, audio_end

    # Mute all strips that are not selected and fall within the audio range
    unselected_strips_in_range = [
        strip
        for strip in bpy.context.scene.sequence_editor.sequences
        if audio_start <= strip.frame_final_start <= audio_end
        and audio_start <= strip.frame_final_end <= audio_end
        and strip not in selected_strips
    ]
    for strip in unselected_strips_in_range:
        strip.mute = True

    # Create the temporary sound mixdown file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        bpy.ops.sound.mixdown(filepath=tmp_file.name, container="WAV", codec="PCM")

    # Unmute the previously muted strips
    for strip in unselected_strips_in_range:
        strip.mute = False

    # Restore the original scene's time range
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = (
        original_frame_start,
        original_frame_end,
    )

    # Ensure the selected strips remain selected (if necessary)
    for strip in selected_strips:
        strip.select = True

    return (audio_start, audio_end, tmp_file.name)


def add_subs(start_frame, srt_file_path):

    # Load the SRT file
    subs = pysubs2.load(srt_file_path)

    # Get the current scene
    scene = bpy.context.scene

    # Ensure there's a sequence in the VSE
    if not scene.sequence_editor:
        scene.sequence_editor_create()
    sequencer = scene.sequence_editor

    next_channel = max((s.channel for s in bpy.context.sequences), default=0) + 1
    text_strip = None

    # Add each subtitle as a text strip
    for sub in tqdm(subs):

        if sub.text.strip() == "":
            continue

        # Calculate the start and end frames based on the subtitle timings and the specified start_frame
        start_frame_sub = start_frame + int((sub.start / 1000) * scene.render.fps)
        end_frame_sub = start_frame + int((sub.end / 1000) * scene.render.fps)

        # print(start_frame_sub, end_frame_sub, sub.text)

        if start_frame_sub == end_frame_sub:
            continue

        # Create a text strip
        text_strip = sequencer.sequences.new_effect(
            name=sub.text,
            type="TEXT",
            channel=next_channel,
            frame_start=start_frame_sub,
            frame_end=end_frame_sub,
        )

        # Set the subtitle text
        tmp_list = []
        for word in sub.text.split():
            if sch.predict(word)[0]:
                tmp_list.append("###")
            else:
                tmp_list.append(word)
        censured_text = " ".join(tmp_list)
        text_strip.text = censured_text
        #

        text_strip.font_size = 70
        text_strip.use_bold = True
        text_strip.use_italic = True
        text_strip.use_shadow = True
        text_strip.use_outline = True
        text_strip.location[0] = 0.5
        text_strip.location[1] = 0.2

    print("Subtitles added successfully!")


class SEQUENCER_OT_mute_audio_profanity(Operator):

    bl_idname = "sequencer.mute_audio_profanity"
    bl_label = "Mute profanity"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene and scene.sequence_editor and scene.sequence_editor.active_strip):
            return False

        if scene.sequence_editor.active_strip.type != "SOUND":
            return False

        selected_strips = get_selected_strips()

        first_channel = selected_strips[0].channel
        return all(strip.channel == first_channel for strip in selected_strips)

    def execute(self, context):
        scene = context.scene
        sequencer = bpy.ops.sequencer

        user_preferences = context.preferences
        addon_prefs = user_preferences.addons[__name__].preferences
        split_type = addon_prefs.split_type

        fps = bpy.context.scene.render.fps

        audio_start, audio_end, tmp_audiofile_path = create_temp_sound_mixdown(
            get_selected_strips()
        )

        server_url = "http://localhost:5302/transcribe"
        transcription_data, srt_file_path = send_audio_for_transcription(
            tmp_audiofile_path, server_url
        )

        next_channel = max((s.channel for s in bpy.context.sequences), default=0) + 1

        if transcription_data:

            # print(transcription_data["segments"])

            for seg in tqdm(transcription_data["segments"]):
                for word in seg["words"]:
                    if sch.predict(word["text"].lower().strip())[0]:
                        tmp_start = int(word["start"] * fps)
                        tmp_end = int(word["end"] * fps)
                        # start
                        context.scene.frame_current = audio_start + tmp_start

                        # sequencer.select_all(action="SELECT")
                        sequencer.split(
                            frame=context.scene.frame_current,
                            type=split_type,
                            side="RIGHT",
                        )

                        sequencer.mute(unselected=False)
                        marker = scene.timeline_markers.new(
                            word["text"] + str(context.scene.frame_current)
                        )
                        marker.frame = context.scene.frame_current
                        # end

                        context.scene.frame_current = audio_start + tmp_end
                        # sequencer.select_all(action="SELECT")
                        sequencer.split(
                            frame=context.scene.frame_current,
                            type=split_type,
                            side="RIGHT",
                        )

                        sequencer.unmute(unselected=False)
                        marker = scene.timeline_markers.new(
                            word["text"] + str(context.scene.frame_current)
                        )
                        marker.frame = context.scene.frame_current

                        #

                        tmp_bass_file = os.path.dirname(__file__) + "/bass.wav"

                        newStrip = context.scene.sequence_editor.sequences.new_sound(
                            name=os.path.basename(tmp_bass_file),
                            filepath=tmp_bass_file,
                            channel=next_channel,
                            frame_start=audio_start + tmp_start,
                        )
                        newStrip.show_waveform = True
                        newStrip.sound.use_mono = True
                        newStrip.volume = 0.05
                        newStrip.animation_offset_start = 5
                        newStrip.frame_final_duration = tmp_end - tmp_start - 1

                        #

            # need to cache swear_check from previous runs, optimize swear_check ( runs twice on same words )
            # move sub generating file functionality here, process words here, then send them to audio bass and subs
            # add_subs(audio_start, srt_file_path) 

        else:
            print("Transcription failed or returned no data")

        os.remove(tmp_audiofile_path)


        return {"FINISHED"}


class SEQUENCER_OT_speechnorm(Operator):

    bl_idname = "sequencer.speechnorm"
    bl_label = "Apply Speechnorm Filter"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene and scene.sequence_editor and scene.sequence_editor.active_strip):
            return False

        if scene.sequence_editor.active_strip.type != "SOUND":
            return False

        return True

    def execute(self, context):

        # Use context manager for tempfile to ensure proper cleanup
        with tempfile.NamedTemporaryFile(
            dir="./audio", suffix=".wav", delete=False
        ) as tmp_file:
            input_filename = os.path.abspath(
                bpy.path.abspath(
                    context.scene.sequence_editor.active_strip.sound.filepath
                )
            )

            command = [
                "ffmpeg",
                "-y",  # Force overwrite
                "-i",
                input_filename,
                "-filter:a",
                "speechnorm",
                tmp_file.name,
            ]

            try:
                subprocess.run(command, check=True)
            except subprocess.CalledProcessError as e:
                self.report({"ERROR"}, f"ffmpeg exited with error: {e}")
                return {"CANCELLED"}

            # Determine the next available channel
            next_channel = max((s.channel for s in context.sequences), default=0) + 1

            # Add new sound strip
            new_strip = context.scene.sequence_editor.sequences.new_sound(
                name=os.path.basename(tmp_file.name),
                filepath=tmp_file.name,
                channel=next_channel,
                frame_start=bpy.context.scene.sequence_editor.active_strip.final_start_frame,
            )
            new_strip.show_waveform = True

        return {"FINISHED"}


# Menu integration
def menu_detect_shots(self, context):
    self.layout.separator()
    self.layout.operator("sequencer.detect_shots")
    self.layout.operator("sequencer.auto_editor_audio")
    self.layout.operator("sequencer.mute_audio_profanity")
    self.layout.operator("sequencer.speechnorm")



def find_segments(arr):
    """
    Finds the start and end indices of consecutive 1s in an array.

    Args:
      arr: The input array.

    Returns:
      A list of tuples, where each tuple represents a segment of 1s
      and contains the start and end indices (inclusive) of the segment.
    """

    segments = []
    start = None
    for i, val in enumerate(arr):
        if val == 1:
            if start is None:
                start = i
        elif start is not None:  # val == 0
            segments.append((start, i - 1))
            start = None

    if start is not None:
        segments.append((start, len(arr) - 1))

    return segments


def process_segmentation_data(data):
    """
    Processes segmentation data from a JSON file.

    Args:
      data: The loaded JSON data.

    Returns:
      A list of lists, where each inner list contains the start and end times
      of a segment in seconds.
    """
    global_segments = []

    for _, v in data.items():
        wav_splits = v["wav_splits"]
        sampling_rate = v["sampling_rate"]
        segments = v["segments"]
        start_time = v["start_time"]

        times = [((s["start"] + s["stop"]) / 2) / sampling_rate for s in wav_splits]
        segment_indices = find_segments(segments)
        # segment_times = [
        #     [start_time + times[start], start_time + times[end]]
        #     for start, end in segment_indices
        # ]
        segment_times = []
        for start, end in segment_indices:
            tmp_start = 0 if times[start] < 4 else times[start]
            segment_start_time = start_time + tmp_start
            segment_end_time = start_time + times[end]
            segment_times.append([segment_start_time, segment_end_time])

        global_segments.append(segment_times)
        # return segment_times  # Return here instead of using exit()
    return global_segments



class SpeechSegmentationProps(bpy.types.PropertyGroup):
    """Property group to store the sample audio path."""

    sample_audio_path: StringProperty(
        name="Sample Audio Path",
        description="Path to the sample audio file",
        default="",
        subtype="FILE_PATH",  # This creates a file selector
    )


class SpeechSegmentationOperator(bpy.types.Operator):
    bl_idname = "vse.speech_segmentation"
    bl_label = "Speech Segmentation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene and scene.sequence_editor and scene.sequence_editor.active_strip):
            return False

        if scene.sequence_editor.active_strip.type != "SOUND":
            return False

        return True

    def execute(self, context):
        strip = context.scene.sequence_editor.active_strip
        props = context.scene.speech_segmentation_props

        server_url = "http://localhost:5303/api/segment"
        sample_audio_path = props.sample_audio_path

        if not sample_audio_path:
            self.report({"ERROR"}, "Please specify a sample audio file path.")
            return {"CANCELLED"}

        main_audio_path = os.path.abspath(
            bpy.path.abspath(context.scene.sequence_editor.active_strip.sound.filepath)
        )

        try:
            response = requests.post(
                server_url,
                data={"main_path": main_audio_path, "sample_path": sample_audio_path},
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error sending request: {e}")
            self.report({"ERROR"}, f"Error sending request: {e}")
            return {"CANCELLED"}

        print(response.json())

        data1 = response.json()
        sd_path = Path(data1["segmentation_data"])

        data2 = None
        with open(data1["segmentation_data"], "r", encoding="utf-8") as f:
            data2 = json.load(f)

        segments_times = process_segmentation_data(data2)

        ffs = context.scene.sequence_editor.active_strip.frame_final_start
        delta = 0.3

        fps = bpy.context.scene.render.fps
        for big_segment in segments_times:
            for times1 in big_segment:
                times_frame_start = int((times1[0] - delta) * fps)
                times_frame_end = int((times1[1] + delta) * fps)

                bpy.ops.sequencer.effect_strip_add(
                    type="COLOR",
                    frame_start=times_frame_start + ffs,
                    frame_end=times_frame_end + ffs,
                    channel=1,
                )

                bpy.ops.sequencer.select_all(action="SELECT")

                bpy.ops.sequencer.split(
                    frame=times_frame_start + ffs,
                    type="SOFT",
                    side="RIGHT",
                )

                bpy.ops.sequencer.select_all(action="SELECT")

                bpy.ops.sequencer.split(
                    frame=times_frame_end + ffs,
                    type="SOFT",
                    side="RIGHT",
                )

        color_strips = []
        sequences = bpy.context.scene.sequence_editor.sequences_all
        for seq in sequences:
            if seq.channel == 1:
                color_strips.append(seq)

        bpy.ops.sequencer.select_all(action="DESELECT")
        for col in color_strips:
            if int((col.frame_final_end - col.frame_final_start) / fps) > 18:
                sequences = bpy.context.scene.sequence_editor.sequences_all

                for seq in sequences:
                    if (
                        seq.frame_final_start >= col.frame_final_start
                        and seq.frame_final_end <= col.frame_final_end
                        and seq.channel != 1
                    ):
                        seq.select = True
                        # color_strips.append(seq)

        self.report({"INFO"}, "Speech segmentation complete!")
        return {"FINISHED"}


class SpeechSegmentationPanel(bpy.types.Panel):
    """Panel for speech segmentation settings."""

    bl_label = "Speech Segmentation"
    bl_idname = "SCENE_PT_speech_segmentation"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Strip"

    def draw(self, context):
        layout = self.layout
        props = context.scene.speech_segmentation_props

        layout.prop(props, "sample_audio_path")  # File path input field
        layout.operator("vse.speech_segmentation")




classes = (
    SEQUENCER_OT_detect_shots,
    SEQUENCER_OT_split_selected,
    SEQUENCER_OT_auto_editor_audio,
    SEQUENCER_OT_mute_audio_profanity,
    SEQUENCER_OT_speechnorm,
    SpeechSegmentationOperator, 
    SpeechSegmentationProps, 
    SpeechSegmentationPanel,
    MyAddonPreferences,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.SEQUENCER_MT_context_menu.append(menu_detect_shots)
    bpy.types.SEQUENCER_MT_strip.append(menu_detect_shots)
    bpy.types.Scene.speech_segmentation_props = bpy.props.PointerProperty(
        type=SpeechSegmentationProps
    )


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.SEQUENCER_MT_context_menu.remove(menu_detect_shots)
    bpy.types.SEQUENCER_MT_strip.remove(menu_detect_shots)
    del bpy.types.Scene.speech_segmentation_props


if __name__ == "__main__":
    register()
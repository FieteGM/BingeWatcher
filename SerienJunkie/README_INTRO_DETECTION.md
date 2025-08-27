# Smart Intro Detection System

## Overview

The BingeWatcher now includes a smart intro detection system that automatically detects when an anime intro is playing and only skips it when appropriate. This prevents skipping during recaps, episode previews, or episodes without intros.

## Features

### 1. Smart Intro Detection
- **Time-based detection**: Uses predefined intro time windows for each anime series
- **Pattern-based detection**: Looks for intro-related keywords in the page content
- **Season-specific data**: Different intro times for different seasons of the same anime
- **Fallback protection**: Only skips when confident an intro is playing

### 2. Dual Input System
- **Start time**: When the intro typically begins (e.g., 85 seconds)
- **End time**: When the intro typically ends (e.g., 145 seconds)
- **Individual control**: Users can adjust both start and end times per series

### 3. Comprehensive Database
The system includes intro times for popular anime series:

#### One Piece (All Seasons)
- **Start**: 85 seconds
- **End**: 145 seconds
- **Detection patterns**: "opening", "intro", "we are", "yo ho ho", "mugiwara", "straw hat"

#### Naruto (All Seasons)
- **Start**: 80 seconds
- **End**: 140 seconds
- **Detection patterns**: "opening", "intro", "naruto", "believe it", "ninja", "hidden leaf"

#### One Punch Man (Seasons 1-2)
- **Start**: 85 seconds
- **End**: 145 seconds
- **Detection patterns**: "opening", "intro", "one punch", "hero", "saitama", "bald"

#### Jujutsu Kaisen (Seasons 1-2)
- **Start**: 85 seconds
- **End**: 145 seconds
- **Detection patterns**: "opening", "intro", "jujutsu", "kaisen", "curse", "sorcerer", "gojo"

#### Attack on Titan (Seasons 1-4)
- **Start**: 80 seconds
- **End**: 140 seconds
- **Detection patterns**: "opening", "intro", "attack", "titan", "shingeki", "eren", "mikasa", "levi"

#### Dandadan (Season 1)
- **Start**: 85 seconds
- **End**: 145 seconds
- **Detection patterns**: "opening", "intro", "dandadan", "supernatural", "alien", "ghost", "momo"

#### Kaiju No. 8 (Season 1)
- **Start**: 85 seconds
- **End**: 145 seconds
- **Detection patterns**: "opening", "intro", "kaiju", "monster", "defense force", "kafka"

## How It Works

### 1. Detection Process
1. **Time Check**: Verifies if current video time is within the intro window
2. **Pattern Analysis**: Searches page content for intro-related keywords
3. **Confidence Assessment**: Only skips if both time and pattern conditions are met
4. **Safe Skip**: Jumps to the end time of the intro

### 2. User Interface
- **Dual Input Fields**: Separate "Start" and "End" time inputs for each series
- **Real-time Updates**: Changes are applied immediately
- **Visual Feedback**: Clear indication of current intro skip settings

### 3. Configuration Files

#### `intro_times.json`
Contains the master database of intro times and detection patterns:
```json
{
  "series-name": {
    "name": "Display Name",
    "intros": [
      {
        "season": 1,
        "start_time": 85,
        "end_time": 145,
        "detection_patterns": ["opening", "intro", "keywords"]
      }
    ],
    "default_skip_start": 85,
    "default_skip_end": 145
  }
}
```

#### `progress.json`
Stores user-specific intro skip times:
```json
{
  "series-name": {
    "intro_skip_start": 85,
    "intro_skip_end": 145
  }
}
```

## Usage

### Automatic Detection
The system automatically detects and skips intros when:
- Auto-skip intro is enabled in settings
- Current video time is within the intro window
- Intro-related patterns are detected in the page

### Manual Adjustment
1. Open the BingeWatcher sidebar
2. Find your series in the list
3. Adjust the "Start" and "End" time inputs
4. Changes are applied automatically

### Adding New Series
To add intro times for a new series:

1. **Edit `intro_times.json`**:
   ```json
   {
     "new-series": {
       "name": "New Series Name",
       "intros": [
         {
           "season": 1,
           "start_time": 90,
           "end_time": 150,
           "detection_patterns": ["opening", "intro", "series-specific-keywords"]
         }
       ],
       "default_skip_start": 90,
       "default_skip_end": 150
     }
   }
   ```

2. **Add to `progress.json`** (optional, will use defaults if not present):
   ```json
   {
     "new-series": {
       "intro_skip_start": 90,
       "intro_skip_end": 150
     }
   }
   ```

## Benefits

1. **Prevents False Skips**: Won't skip during recaps or episode previews
2. **Season Awareness**: Handles different intro times per season
3. **User Customization**: Individual control over skip times
4. **Smart Fallback**: Uses pattern detection as backup to time-based detection
5. **Easy Maintenance**: Centralized database for intro times

## Technical Details

### Detection Algorithm
```python
def detect_intro_start(driver, series: str, season: int = 1) -> bool:
    # 1. Get current video time
    # 2. Check if within intro time window
    # 3. Search page for detection patterns
    # 4. Return True if both conditions met
```

### Smart Skip Function
```python
def smart_skip_intro(driver, series: str, season: int = 1):
    # 1. Wait for video to be ready
    # 2. Get intro times from database
    # 3. Detect if intro is playing
    # 4. Skip to end time if detected
    # 5. Fall back to simple skip if detection fails
```

This system provides a much more intelligent and user-friendly approach to intro skipping, ensuring that users only skip actual intros and not other content.

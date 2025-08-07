# BingeWatcher - Enhanced Series Streaming Bot

A robust and feature-rich automated streaming bot for s.to that provides seamless binge-watching experience with progress tracking, automatic episode progression, and a user-friendly sidebar interface.

## Features

### 🎯 Core Functionality
- **Automatic Episode Progression**: Seamlessly moves to the next episode when current one ends
- **Progress Tracking**: Saves your position in each series for resuming later
- **Smart Intro Skipping**: Automatically skips intros for episodes longer than 5 minutes
- **Fullscreen Support**: Automatically enables fullscreen mode for immersive viewing
- **Robust Error Handling**: Comprehensive error recovery and retry mechanisms

### 🎨 User Interface
- **Interactive Sidebar**: Modern, responsive sidebar with hover effects
- **Series Management**: Easy deletion of series from your watchlist
- **Quick Actions**: Skip to end, quit application, and series selection
- **Real-time Progress**: Live display of remaining time and current position

### 🔧 Technical Improvements
- **Enhanced Stability**: Better browser management and error recovery
- **Atomic File Operations**: Safe progress saving with backup creation
- **Retry Logic**: Automatic retry for failed operations
- **State Management**: Proper global state tracking
- **Memory Management**: Efficient resource usage and cleanup

## Installation

### Prerequisites
- Python 3.8 or higher
- Firefox browser
- GeckoDriver (included in the package)

### Setup
1. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Verify GeckoDriver**:
   - Ensure `geckodriver.exe` is in the same directory as the script
   - The script will automatically check for its presence

3. **Firefox Profile**:
   - The script creates a custom Firefox profile in `user.BingeWatcher/`
   - This profile is automatically managed and optimized for streaming

## Usage

### Basic Usage
```bash
python s.toBot.py
```

### Configuration Options
Edit the configuration section at the top of `s.toBot.py`:

```python
# === CONFIGURATION ===
HEADLESS = False          # Set to True for headless mode
START_URL = 'https://s.to/'
INTRO_SKIP_SECONDS = 320  # Skip intro after 5 minutes 20 seconds
MAX_RETRIES = 3          # Number of retry attempts for failed operations
WAIT_TIMEOUT = 15        # Timeout for page loading (seconds)
PROGRESS_SAVE_INTERVAL = 5  # Save progress every 5 seconds
```

### How It Works

1. **Startup**: The script launches Firefox with optimized settings
2. **Navigation**: Automatically navigates to s.to
3. **Sidebar Injection**: Injects the interactive sidebar for series management
4. **Episode Detection**: Auto-detects if you're on an episode page
5. **Playback Loop**: 
   - Switches to video iframe
   - Starts video playback
   - Enables fullscreen
   - Skips intro if applicable
   - Monitors playback progress
   - Saves position periodically
   - Moves to next episode when current ends

### Sidebar Features

#### Series List
- Shows all series in your watchlist
- Displays current season, episode, and position
- Click any series to jump to that episode

#### Quick Actions
- **Skip ▶**: Skip current episode to end
- **Close ✕**: Quit the application
- **✕** (next to series): Delete series from watchlist

#### Visual Feedback
- Hover effects on series items
- Real-time progress updates
- Modern gradient styling

## File Structure

```
SerienJunkie/
├── s.toBot.py              # Main script
├── requirements.txt         # Python dependencies
├── README.md              # This file
├── geckodriver.exe        # Firefox WebDriver
├── progress.json          # Progress database (auto-created)
├── progress.json.backup   # Backup of progress (auto-created)
└── user.BingeWatcher/     # Firefox profile (auto-created)
```

## Error Handling

The script includes comprehensive error handling for:

- **Browser Issues**: Automatic retry for browser startup failures
- **Network Problems**: Retry logic for navigation failures
- **Video Playback**: Multiple methods for starting video playback
- **File Operations**: Safe progress saving with backup creation
- **Element Interactions**: Fallback methods for clicking and interaction
- **State Recovery**: Automatic recovery from unexpected states

## Troubleshooting

### Common Issues

1. **GeckoDriver not found**:
   - Ensure `geckodriver.exe` is in the same directory as the script
   - Download from: https://github.com/mozilla/geckodriver/releases

2. **Firefox not starting**:
   - Check if Firefox is installed and accessible
   - Try running in non-headless mode first

3. **Video not playing**:
   - Check internet connection
   - Verify the episode URL is accessible
   - Try refreshing the page manually

4. **Sidebar not appearing**:
   - Check browser console for JavaScript errors
   - Ensure the page has fully loaded
   - Try refreshing the page

### Debug Mode
Enable detailed logging by modifying the logging level:
```python
logging.basicConfig(
    format='[BingeWatcher] %(levelname)s: %(message)s',
    level=logging.DEBUG  # Change from INFO to DEBUG
)
```

## Safety Features

- **Progress Backup**: Automatic backup creation before saving
- **Graceful Shutdown**: Proper cleanup on exit
- **Resource Management**: Efficient memory and CPU usage
- **Error Recovery**: Automatic recovery from most errors
- **State Persistence**: Progress is saved even if script crashes

## Performance Optimizations

- **Efficient Monitoring**: Progress saved at intervals, not continuously
- **Smart Retries**: Limited retry attempts to prevent infinite loops
- **Resource Cleanup**: Proper cleanup of browser resources
- **Optimized Selectors**: Fast and reliable element selection

## Contributing

Feel free to submit issues and enhancement requests. The code is designed to be modular and extensible.

## License

This project is for educational purposes. Please respect the terms of service of the streaming platform.

## Changelog

### Version 2.0 (Current)
- Complete rewrite with enhanced error handling
- Improved sidebar with modern styling
- Better state management and recovery
- Atomic file operations with backup
- Comprehensive retry logic
- Enhanced video playback reliability
- Better progress tracking and display

### Version 1.0 (Original)
- Basic streaming functionality
- Simple progress tracking
- Basic sidebar interface

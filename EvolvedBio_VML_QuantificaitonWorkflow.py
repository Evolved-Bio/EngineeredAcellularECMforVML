# **Step 1:** Loading and processing SVG files in the environment


!pip install cairosvg

import os
import re
import logging
import xml.etree.ElementTree as ET
import cairosvg
from pathlib import Path
import pandas as pd
from google.colab import drive
import io
import tempfile
from IPython.display import display
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
import multiprocessing

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def authenticate_drive():
    """Authenticate and create Drive API service."""
    try:
        auth.authenticate_user()
        drive_service = build('drive', 'v3')
        return drive_service
    except Exception as e:
        logger.error(f"Authentication failed: {str(e)}")
        return None

def select_folder():
    """Prompt user to enter folder path."""
    from pathlib import Path
    while True:
        folder_path = input('Please enter the path to the folder containing your SVG files (e.g., /content/drive/MyDrive/your_folder): ')
        folder_path = folder_path.strip()
        if Path(folder_path).exists():
            folder_name = os.path.basename(folder_path)
            print(f"Selected folder: {folder_name}")
            return {'id': None, 'name': folder_name, 'path': folder_path}
        else:
            print("Folder not found. Please try again.")

def get_files_from_folder(folder_info):
    """Get SVG files from the specified folder."""
    folder_path = folder_info['path']
    svg_files = []
    svg_files = [
        {'name': f, 'path': os.path.join(folder_path, f)}
        for f in os.listdir(folder_path)
        if f.lower().endswith('.svg')
    ]
    if not svg_files:
        logger.error("No SVG files found in the selected folder.")
    return svg_files

def download_svg_file(file_info):
    """Read an SVG file from the folder."""
    try:
        with open(file_info['path'], 'rb') as f:
            return io.BytesIO(f.read())
    except Exception as e:
        logger.error(f"Error reading file: {str(e)}")
        return None

# Compile regex pattern once
FILENAME_PATTERN = re.compile(r'([^-]+)\s*-\s*Week\s*(\d+)\s*-\s*([^-]+)\s*-\s*([^-]+)\s*-\s*Animal\s*(\d+)')

def extract_metadata(filename):
    """Extract metadata from filename."""
    match = FILENAME_PATTERN.match(filename)
    if match:
        condition, week, staining, location, animal = match.groups()
        return {
            'Filename': filename,
            'Condition': condition.strip(),
            'Week': int(week),
            'Staining': staining.strip(),
            'Location': location.strip(),
            'Animal': int(animal)
        }
    else:
        logger.warning(f"Filename format error: {filename}")
        return None

# Define namespaces at module level to avoid recreation
NAMESPACES = {
    'svg': 'http://www.w3.org/2000/svg',
    'inkscape': 'http://www.inkscape.org/namespaces/inkscape',
    'xlink': 'http://www.w3.org/1999/xlink'
}

def process_single_element(element_info, root, width, height, base_name, output_dir, metadata):
    """Process a single SVG element."""
    elem_type, id_type, id_value, subdir, suffix, needs_background, meta_key = element_info

    stain_subdir = output_dir / subdir / metadata['Staining']
    stain_subdir.mkdir(parents=True, exist_ok=True)

    new_root = ET.Element('svg')
    new_root.set('xmlns', 'http://www.w3.org/2000/svg')
    new_root.set('width', f'{width}px')
    new_root.set('height', f'{height}px')
    new_root.set('viewBox', f'0 0 {width} {height}')

    if id_type == 'inkscape_label':
        element = root.find(f".//svg:{elem_type}[@{{{NAMESPACES['inkscape']}}}label='{id_value}']", NAMESPACES)
    else:
        element = root.find(f".//svg:{elem_type}[@id='{id_value}']", NAMESPACES)

    if element is not None:
        if needs_background:
            background = ET.SubElement(new_root, 'rect')
            background.set('width', f'{width}px')
            background.set('height', f'{height}px')
            background.set('fill', 'white')
            background.set('x', '0')
            background.set('y', '0')

        new_elem = ET.SubElement(new_root, elem_type)

        if elem_type == 'image':
            # Special handling for image elements
            # Set dimensions first
            new_elem.set('width', f'{width}px')
            new_elem.set('height', f'{height}px')

            # Explicitly set position
            new_elem.set('x', '0')
            new_elem.set('y', '0')

            # Set preserveAspectRatio to none to ensure image fills the space
            new_elem.set('preserveAspectRatio', 'none')

            # Copy the image data and other attributes
            for attr, value in element.attrib.items():
                if attr == '{http://www.w3.org/1999/xlink}href':
                    new_elem.set(attr, value)
                elif attr not in ['width', 'height', 'x', 'y', 'preserveAspectRatio']:
                    new_elem.set(attr, value)
        else:
            # Handle non-image elements (ROI and Grid)
            for attr, value in element.attrib.items():
                if attr in ['width', 'height']:
                    new_elem.set(attr, f'{width}px')
                else:
                    new_elem.set(attr, value)

            # Copy all child elements for non-image elements
            for child in element:
                new_elem.append(ET.fromstring(ET.tostring(child)))

        with tempfile.NamedTemporaryFile(suffix='.svg', delete=False) as temp_svg:
            tree = ET.ElementTree(new_root)
            tree.write(temp_svg.name)

        output_filename = f"{base_name}{suffix}.png"
        output_path = stain_subdir / output_filename

        try:
            cairosvg.svg2png(
                file_obj=open(temp_svg.name, 'rb'),
                write_to=str(output_path),
                output_width=width,
                output_height=height,
                dpi=250
            )
            result = {
                'path': str(output_path.relative_to(output_dir)),
                'dimensions': f"{width}x{height}",
                'status': 'SUCCESS'
            }
        except Exception as e:
            logger.error(f'Error converting to PNG: {str(e)}')
            result = {'path': "CONVERSION_ERROR", 'dimensions': None, 'status': 'ERROR'}

        os.unlink(temp_svg.name)
        return id_value, result
    return id_value, {'path': "MISSING", 'dimensions': None, 'status': 'MISSING'}

def process_svg_file(svg_content, filename, output_dir):
    """Process a single SVG file and extract layers."""
    base_name = Path(filename).stem
    metadata = extract_metadata(base_name)
    if not metadata:
        return None

    try:
        tree = ET.parse(io.BytesIO(svg_content.getvalue()))
        root = tree.getroot()

        original_image = root.find(f".//svg:image[@id='image2']", NAMESPACES)
        if original_image is None:
            logger.error(f"Original image (image2) not found in {filename}. Cannot proceed.")
            return None

        # Initialize width and height as None
        width = height = None

        # First try to get dimensions from viewBox as it's most reliable
        viewbox = root.get('viewBox')
        if viewbox:
            try:
                parts = viewbox.split()
                if len(parts) == 4:
                    width = int(float(parts[2]))
                    height = int(float(parts[3]))
            except (ValueError, TypeError, IndexError):
                width = height = None

        # If viewBox dimensions aren't available, fall back to image dimensions
        if not width or not height:
            def parse_dimension(value):
                if value is None:
                    return None
                value = re.sub(r'[^0-9.]', '', str(value))
                try:
                    return int(float(value))
                except (ValueError, TypeError):
                    return None

            width = parse_dimension(original_image.get('width'))
            height = parse_dimension(original_image.get('height'))

            # Last resort: try root SVG dimensions
            if width is None:
                width = parse_dimension(root.get('width'))
            if height is None:
                height = parse_dimension(root.get('height'))

        if not width or not height:
            logger.error(f"Could not determine valid dimensions for {filename}")
            return None

        # Rest of the function remains exactly the same
        elements_to_process = [
            ('image', 'id', 'image2', 'Original-Images', '', False, 'OriginalPath'),
            ('g', 'id', 'g3', 'ROI-Images', ' (ROI)', True, 'ROIPath'),
            ('g', 'inkscape_label', 'Grids', 'Grid-Images', ' (Grid)', True, 'GridPath')
        ]

        with ThreadPoolExecutor() as executor:
            process_func = partial(process_single_element, root=root, width=width, height=height,
                                 base_name=base_name, output_dir=output_dir, metadata=metadata)
            futures = [executor.submit(process_func, element_info) for element_info in elements_to_process]

            results = {}
            for future in as_completed(futures):
                id_value, result = future.result()
                results[id_value] = result

        # Update metadata with results
        all_missing = True
        missing_elements = []

        for elem_info in elements_to_process:
            id_value = elem_info[2]
            meta_key = elem_info[6]
            result = results[id_value]

            if result['status'] != 'MISSING':
                all_missing = False
            else:
                missing_elements.append(id_value)

            metadata[meta_key] = result['path']
            if result['dimensions']:
                metadata[f'{meta_key}_dimensions'] = result['dimensions']

        if all_missing:
            metadata['processing_status'] = "NO_ELEMENTS_FOUND"
        elif missing_elements:
            metadata['processing_status'] = f"MISSING_ELEMENTS: {', '.join(missing_elements)}"
        else:
            metadata['processing_status'] = "SUCCESS"

        return metadata

    except Exception as e:
        logger.error(f'Error processing {filename}: {str(e)}')
        return None

def main():
    """Main function to process SVG files from Google Drive."""
    try:
        drive.mount('/content/drive')
    except Exception as e:
        logger.info("Drive already mounted")

    folder_info = select_folder()
    if not folder_info:
        logger.error("No folder was selected")
        return

    os.environ['PROCESSED_FOLDER_PATH'] = folder_info['path']
    output_dir = Path(folder_info['path'])
    logger.info(f"Selected folder: {folder_info['name']}")

    svg_files = get_files_from_folder(folder_info)
    if not svg_files:
        logger.error("No SVG files found for processing!")
        return

    metadata_list = []
    files_with_issues = []

    # Calculate optimal number of workers based on CPU cores
    max_workers = min(32, (multiprocessing.cpu_count() * 2))

    # Process files in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(download_svg_file, file): file
            for file in svg_files
        }

        # Use tqdm to show progress
        with tqdm(total=len(svg_files), desc="Processing SVG files") as pbar:
            for future in as_completed(future_to_file):
                file = future_to_file[future]
                svg_content = future.result()

                if svg_content:
                    metadata = process_svg_file(svg_content, file['name'], output_dir)
                    if metadata:
                        metadata_list.append(metadata)
                        if metadata['processing_status'] != "SUCCESS":
                            files_with_issues.append((file['name'], metadata['processing_status']))

                pbar.update(1)

    if metadata_list:
        df = pd.DataFrame(metadata_list)
        metadata_path = output_dir / 'metadata.csv'
        df.to_csv(metadata_path, index=False)
        logger.info(f"\nMetadata saved to metadata.csv in {folder_info['name']}")

        logger.info("\nProcessing Summary:")
        logger.info(f"Total files processed: {len(metadata_list)}")
        logger.info(f"Files with all components: {len(metadata_list) - len(files_with_issues)}")

        if files_with_issues:
            logger.info("\nFiles with missing components:")
            for filename, status in files_with_issues:
                logger.info(f"- {filename}: {status}")

        logger.info("\nProcessed Files Summary:")
        print("")
        display_columns = ['Condition', 'Week', 'Staining', 'Location', 'Animal', 'processing_status']
        print(df[display_columns])

        logger.info(f"\nProcessing complete. Folder path stored for next script: {folder_info['path']}")
    else:
        logger.warning("No files were successfully processed")

if __name__ == "__main__":
    main()




"""# **Step 2:** Prosessing images using ROI and Grid lines"""


import cv2
import numpy as np
from pathlib import Path
import matplotlib.pyplot as plt
import logging
import pandas as pd
import os
from tqdm import tqdm
from google.colab import drive
import re
import gc
import psutil

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_memory():
    """Log current memory usage."""
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    logger.info(f"Memory usage: {memory_mb:.2f} MB")

def sanitize_filename(name):
    """Sanitize filename by removing invalid characters."""
    return re.sub(r'[^\w\-_\. ]', '_', name)

def create_roi_mask(roi_image):
    """Create mask from ROI image with black contour on white background."""
    try:
        # Convert to grayscale if needed
        if len(roi_image.shape) == 3:
            roi_gray = cv2.cvtColor(roi_image, cv2.COLOR_BGR2GRAY)
            del roi_image  # Immediate cleanup
        else:
            roi_gray = roi_image.copy()
            del roi_image

        # Use adaptive thresholding for better results
        binary = cv2.adaptiveThreshold(
            roi_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 11, 2
        )
        del roi_gray  # Cleanup grayscale image

        # Find contours
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros_like(binary)
        del binary  # Cleanup binary image

        if contours:
            cv2.drawContours(mask, contours, -1, (255), thickness=cv2.FILLED)
        else:
            logger.warning("No contours found in ROI image.")

        del contours  # Cleanup contours
        return mask
    except Exception as e:
        logger.error(f"Error in create_roi_mask: {e}")
        return None
    finally:
        gc.collect()

def apply_roi_mask(original_image, mask):
    """Apply ROI mask to original image."""
    try:
        # Ensure mask is binary
        mask_binary = mask.astype(bool)
        del mask  # Original mask no longer needed

        # Create result array
        result = np.full_like(original_image, 255)

        # Apply mask
        result[mask_binary] = original_image[mask_binary]
        del mask_binary  # Cleanup mask

        return result
    except Exception as e:
        logger.error(f"Error in apply_roi_mask: {e}")
        return None
    finally:
        gc.collect()

def visualize_roi_process(roi_image, original_image, result, output_path):
    """Visualize original, ROI, and masked result and save to file."""
    try:
        plt.close('all')  # Ensure all previous plots are closed
        fig, axs = plt.subplots(1, 3, figsize=(15, 5))

        # Process one image at a time
        # Original image
        rgb_original = cv2.cvtColor(original_image, cv2.COLOR_BGR2RGB)
        axs[0].imshow(rgb_original)
        axs[0].set_title('Original Image')
        axs[0].axis('off')
        del rgb_original

        # ROI contour
        rgb_roi = cv2.cvtColor(roi_image, cv2.COLOR_BGR2RGB)
        axs[1].imshow(rgb_roi)
        axs[1].set_title('ROI Contour')
        axs[1].axis('off')
        del rgb_roi

        # Final masked result
        rgb_result = cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
        axs[2].imshow(rgb_result)
        axs[2].set_title('Masked Result')
        axs[2].axis('off')
        del rgb_result

        plt.tight_layout()
        plt.savefig(str(output_path), bbox_inches='tight', dpi=250)
    except Exception as e:
        logger.error(f"Error in visualize_roi_process: {e}")
    finally:
        plt.close('all')
        gc.collect()

def process_single_image(orig_path, roi_path, filename):
    """Process a single image pair."""
    try:
        # Read images one at a time
        orig_img = cv2.imread(str(orig_path))
        if orig_img is None:
            logger.error(f"Failed to read original image: {filename}")
            return None, None, None

        roi_img = cv2.imread(str(roi_path))
        if roi_img is None:
            logger.error(f"Failed to read ROI image: {filename}")
            del orig_img
            return None, None, None

        # Check sizes
        if orig_img.shape != roi_img.shape:
            logger.error(f"Size mismatch: Original {orig_img.shape} vs ROI {roi_img.shape} for {filename}")
            return None, None, None

        # Create mask
        mask = create_roi_mask(roi_img.copy())
        if mask is None:
            return None, None, None

        # Apply mask
        result = apply_roi_mask(orig_img, mask)
        del mask  # Cleanup mask

        if result is None:
            return None, None, None

        return orig_img, roi_img, result
    except Exception as e:
        logger.error(f"Error in process_single_image for {filename}: {e}")
        return None, None, None
    finally:
        gc.collect()

def process_staining_group(stain_df, base_path, output_dir, vis_dir, metadata_df):
    """Process a group of images with the same staining."""
    try:
        for idx in tqdm(stain_df.index, desc=f"Processing {stain_df.iloc[0]['Staining']} images"):
            row = stain_df.loc[idx]

            # Check if files exist
            orig_path = base_path / row['OriginalPath']
            roi_path = base_path / row['ROIPath']

            if not (orig_path.exists() and roi_path.exists()):
                logger.warning(f"Missing files for {row['Filename']}")
                continue

            # Process single image pair
            orig_img, roi_img, result = process_single_image(orig_path, roi_path, row['Filename'])

            if result is not None:
                try:
                    # Create output filename
                    output_name = f"{row['Condition']}_Week{row['Week']}_{row['Staining']}_{row['Location']}_Animal{row['Animal']}_masked.png"
                    output_name = sanitize_filename(output_name)
                    output_path = output_dir / output_name

                    # Save result
                    cv2.imwrite(str(output_path), result)

                    # Create and save visualization
                    vis_output_name = output_name.replace('.png', '_visualization.png')
                    vis_output_path = vis_dir / vis_output_name
                    visualize_roi_process(roi_img, orig_img, result, vis_output_path)

                    # Update metadata
                    metadata_df.at[idx, 'MaskedPath'] = str(output_path.relative_to(base_path))
                    metadata_df.at[idx, 'ROIVisualizationPath'] = str(vis_output_path.relative_to(base_path))

                    logger.info(f"Processed {output_name}")
                finally:
                    # Cleanup
                    del orig_img, roi_img, result
                    gc.collect()
            else:
                logger.error(f"Processing failed for {row['Filename']}")

            # Log memory usage periodically
            if idx % 5 == 0:
                log_memory()

    except Exception as e:
        logger.error(f"Error in process_staining_group: {e}")
    finally:
        plt.close('all')
        gc.collect()

def process_all_images_from_metadata(drive_folder_path):
    """Process all images using metadata CSV file."""
    try:
        base_path = Path(drive_folder_path)
        metadata_path = base_path / 'metadata.csv'

        if not metadata_path.exists():
            logger.error(f"Metadata file not found at: {metadata_path}")
            return None

        # Read metadata
        metadata_df = pd.read_csv(metadata_path)
        logger.info(f"Found {len(metadata_df)} images in metadata")

        # Verify columns
        required_columns = ['OriginalPath', 'ROIPath', 'Filename', 'Condition', 'Week', 'Staining', 'Location', 'Animal']
        missing_columns = [col for col in required_columns if col not in metadata_df.columns]
        if missing_columns:
            logger.error(f"Missing required columns in metadata: {missing_columns}")
            return None

        # Create directories
        output_dir = base_path / 'Masked-Results'
        vis_dir = base_path / 'ROIVisualizations'
        output_dir.mkdir(parents=True, exist_ok=True)
        vis_dir.mkdir(parents=True, exist_ok=True)

        # Process each staining type
        stain_types = metadata_df['Staining'].dropna().unique()
        logger.info(f"Found staining types: {stain_types}")

        for stain in stain_types:
            try:
                print(f"\nNow processing {stain} staining...")
                log_memory()

                # Create stain-specific directories
                stain_output_dir = output_dir / sanitize_filename(stain)
                stain_vis_dir = vis_dir / sanitize_filename(stain)
                stain_output_dir.mkdir(parents=True, exist_ok=True)
                stain_vis_dir.mkdir(parents=True, exist_ok=True)

                # Get stain-specific data
                stain_df = metadata_df[metadata_df['Staining'] == stain].copy()

                # Process staining group
                process_staining_group(stain_df, base_path, stain_output_dir, stain_vis_dir, metadata_df)

                # Save progress after each staining type
                metadata_df.to_csv(metadata_path, index=False)

                # Cleanup
                del stain_df
                gc.collect()
                log_memory()

            except Exception as e:
                logger.error(f"Error processing staining type {stain}: {e}")
                continue

        return metadata_path

    except Exception as e:
        logger.error(f"Error in process_all_images_from_metadata: {e}")
        return None
    finally:
        plt.close('all')
        gc.collect()

def main():
    try:
        # Mount Google Drive
        try:
            drive.mount('/content/drive')
        except Exception as e:
            logger.info("Drive already mounted")

        # Get folder path
        folder_path = os.getenv('PROCESSED_FOLDER_PATH')
        if not folder_path:
            logger.error("Previous folder path not found! Please run the SVG processing script first.")
            return

        base_path = Path(folder_path)
        if not base_path.exists() or not (base_path / 'metadata.csv').exists():
            logger.error(f"Metadata file not found in: {folder_path}")
            return

        logger.info(f"Using previously processed folder: {folder_path}")

        # Process images
        processed_results = process_all_images_from_metadata(folder_path)

        if processed_results:
            logger.info("Processing completed successfully!")
        else:
            logger.error("Processing failed!")

    except Exception as e:
        logger.error(f"Error in main: {e}")
    finally:
        plt.close('all')
        gc.collect()

if __name__ == "__main__":
    main()


"""Tiling ROIs using Grid lines"""

import cv2
import numpy as np
from pathlib import Path
import matplotlib.pyplot as plt
import logging
import pandas as pd
import os
from tqdm import tqdm
from google.colab import drive
import gc
import psutil
import platform
import time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_memory():
    """Log current memory usage."""
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    logger.info(f"Memory usage: {memory_mb:.2f} MB")

def enhance_grid_lines(grid_image):
    """Enhance grid lines using multiple preprocessing techniques."""
    try:
        if len(grid_image.shape) == 3:
            gray = cv2.cvtColor(grid_image, cv2.COLOR_BGR2GRAY)
        else:
            gray = grid_image.copy()

        combined = np.zeros_like(gray)

        # Process methods one at a time to save memory
        # Method 1: Adaptive thresholding
        adaptive1 = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 11, 2
        )
        combined = cv2.bitwise_or(combined, adaptive1)
        del adaptive1

        # Method 2: Different adaptive parameters
        adaptive2 = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 21, 4
        )
        combined = cv2.bitwise_or(combined, adaptive2)
        del adaptive2

        # Method 3: Edge detection
        edges = cv2.Canny(gray, 50, 150)
        combined = cv2.bitwise_or(combined, edges)
        del edges

        # Method 4: Multiple thresholds
        for thresh in [50, 100, 150]:
            _, binary = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY_INV)
            combined = cv2.bitwise_or(combined, binary)
            del binary

        del gray
        return combined
    except Exception as e:
        logger.error(f"Error in enhance_grid_lines: {e}")
        return None
    finally:
        gc.collect()

def cluster_lines(lines, tolerance=20, filename=None):
    """Cluster nearby lines."""
    if not lines:
        if filename:
            logger.warning(f"No lines to cluster in file: {filename}")
        return []

    try:
        lines = np.array(sorted(lines))
        clusters = []
        current_cluster = [lines[0]]

        for line in lines[1:]:
            if line - current_cluster[-1] <= tolerance:
                current_cluster.append(line)
            else:
                clusters.append(int(np.mean(current_cluster)))
                current_cluster = [line]

        if current_cluster:
            clusters.append(int(np.mean(current_cluster)))

        return sorted(clusters)
    except Exception as e:
        logger.error(f"Error in cluster_lines: {e}")
        return []
    finally:
        gc.collect()

def detect_grid_lines(grid_image, min_line_length_ratio=0.3, filename=None):
    """Detect grid lines with adaptive parameters."""
    try:
        height, width = grid_image.shape[:2]
        min_line_length = min(height, width) * min_line_length_ratio

        # Enhance grid lines
        enhanced = enhance_grid_lines(grid_image)
        if enhanced is None:
            return [], [], None

        # Process lines in batches to save memory
        h_lines = []
        v_lines = []
        angle_threshold = 20

        for threshold in [50, 100, 150]:
            for min_gap in [5, 10, 20]:
                lines = cv2.HoughLinesP(
                    enhanced,
                    rho=1,
                    theta=np.pi/180,
                    threshold=threshold,
                    minLineLength=min_line_length,
                    maxLineGap=min_gap
                )

                if lines is not None:
                    for line in lines:
                        x1, y1, x2, y2 = line[0]
                        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))

                        if angle < angle_threshold or angle > 180 - angle_threshold:
                            h_lines.append((y1 + y2) / 2)
                        elif abs(angle - 90) < angle_threshold:
                            v_lines.append((x1 + x2) / 2)

                    del lines

        del enhanced

        # Cluster lines
        h_clusters = cluster_lines(h_lines, filename=filename)
        v_clusters = cluster_lines(v_lines, filename=filename)

        # Create debug image
        debug_img = cv2.cvtColor(grid_image, cv2.COLOR_BGR2RGB) if len(grid_image.shape) == 3 else cv2.cvtColor(grid_image, cv2.COLOR_GRAY2RGB)

        # Draw lines
        for y in h_clusters:
            cv2.line(debug_img, (0, int(y)), (width, int(y)), (0, 0, 255), 2)
        for x in v_clusters:
            cv2.line(debug_img, (int(x), 0), (int(x), height), (255, 0, 0), 2)

        return h_clusters, v_clusters, debug_img

    except Exception as e:
        logger.error(f"Error in detect_grid_lines for {filename}: {e}")
        return [], [], None
    finally:
        gc.collect()

def create_tiles_from_masked(masked_image, h_lines, v_lines):
    """Create tiles from masked image using detected grid lines."""
    try:
        height, width = masked_image.shape[:2]

        # Add boundaries
        h_lines = sorted([0] + list(h_lines) + [height])
        v_lines = sorted([0] + list(v_lines) + [width])

        tiles = []
        min_tile_size = 50
        min_content_ratio = 0.1

        # Process one tile at a time
        for i in range(len(h_lines) - 1):
            for j in range(len(v_lines) - 1):
                y1, y2 = int(h_lines[i]), int(h_lines[i + 1])
                x1, x2 = int(v_lines[j]), int(v_lines[j + 1])

                # Extract tile
                tile = masked_image[y1:y2, x1:x2].copy()

                # Check size
                if tile.shape[0] < min_tile_size or tile.shape[1] < min_tile_size:
                    del tile
                    continue

                # Check content
                if len(tile.shape) == 3:
                    non_white = np.any(tile != [255, 255, 255], axis=2)
                else:
                    non_white = tile != 255

                content_ratio = np.mean(non_white)
                del non_white

                if content_ratio > min_content_ratio:
                    tiles.append(tile)
                else:
                    del tile

        return tiles

    except Exception as e:
        logger.error(f"Error in create_tiles_from_masked: {e}")
        return []
    finally:
        gc.collect()

def calculate_roi_area(masked_image, h_lines, v_lines, grid_spacing_microns=500):
    """Calculate the area of the ROI using grid lines as scale reference."""
    try:
        # Calculate grid sizes
        h_spaces = np.diff(sorted(h_lines))
        v_spaces = np.diff(sorted(v_lines))

        h_median = np.median(h_spaces)
        v_median = np.median(v_spaces)

        # Calculate valid spaces
        valid_h_spaces = h_spaces[abs(h_spaces - h_median) < h_median * 0.5]
        valid_v_spaces = v_spaces[abs(v_spaces - v_median) < v_median * 0.5]

        avg_grid_size_pixels = np.mean([np.mean(valid_h_spaces), np.mean(valid_v_spaces)])
        microns_per_pixel = grid_spacing_microns / avg_grid_size_pixels

        # Clean up spacing arrays
        del h_spaces, v_spaces, valid_h_spaces, valid_v_spaces

        # Create ROI mask
        if len(masked_image.shape) == 3:
            gray = cv2.cvtColor(masked_image, cv2.COLOR_BGR2GRAY)
        else:
            gray = masked_image.copy()

        roi_mask = (gray < 255).astype(np.uint8)
        del gray

        pixel_count = np.count_nonzero(roi_mask)
        area_sq_microns = pixel_count * (microns_per_pixel ** 2)

        # Create visualization
        vis_img = cv2.cvtColor(masked_image, cv2.COLOR_BGR2RGB)

        for y in h_lines:
            cv2.line(vis_img, (0, int(y)), (vis_img.shape[1], int(y)), (0, 255, 0), 1)
        for x in v_lines:
            cv2.line(vis_img, (int(x), 0), (int(x), vis_img.shape[0]), (0, 255, 0), 1)

        # Handle different OpenCV versions for findContours
        findContours_output = cv2.findContours(roi_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(findContours_output) == 3:
            _, contours, _ = findContours_output
        else:
            contours, _ = findContours_output

        cv2.drawContours(vis_img, contours, -1, (255, 0, 0), 2)

        del roi_mask, contours

        return area_sq_microns, vis_img

    except Exception as e:
        logger.error(f"Error in calculate_roi_area: {e}")
        return None, None
    finally:
        gc.collect()

def visualize_tile_process(masked_img, debug_img, tiles, metadata_row, vis_dir):
    """Visualize original image, grid lines, and all tiles."""
    try:
        plt.close('all')
        n_total = 2 + len(tiles)
        fig, axs = plt.subplots(1, n_total, figsize=(5 * min(n_total, 10), 5))

        # Process one image at a time
        rgb_img = cv2.cvtColor(masked_img, cv2.COLOR_BGR2RGB)
        axs[0].imshow(rgb_img)
        axs[0].set_title('Original Masked Image')
        axs[0].axis('off')
        del rgb_img

        axs[1].imshow(debug_img)
        axs[1].set_title('Detected Grid')
        axs[1].axis('off')

        for idx, tile in enumerate(tiles):
            tile_rgb = cv2.cvtColor(tile, cv2.COLOR_BGR2RGB)
            axs[idx + 2].imshow(tile_rgb)
            axs[idx + 2].set_title(f'Tile {idx + 1}')
            axs[idx + 2].axis('off')
            del tile_rgb

        plt.tight_layout()
        vis_path = vis_dir / f"{Path(metadata_row['Filename']).stem}_grid_visualization.png"
        plt.savefig(str(vis_path), bbox_inches='tight', dpi=250)

        # Clean up plot objects
        plt.close(fig)
        del fig, axs

    except Exception as e:
        logger.error(f"Error in visualization for {metadata_row['Filename']}: {e}")
    finally:
        plt.close('all')
        gc.collect()

def process_single_image(masked_path, grid_path, metadata_row, base_path, vis_dir, tiles_dir):
    """Process a single image and its grid."""
    try:
        # Read images
        masked_img = cv2.imread(str(masked_path))
        grid_img = cv2.imread(str(grid_path))

        if masked_img is None or grid_img is None:
            logger.error(f"Failed to read images for {metadata_row['Filename']}")
            return None

        if masked_img.shape[:2] != grid_img.shape[:2]:
            logger.error(f"Size mismatch for {metadata_row['Filename']}")
            return None

        # Detect grid lines and get visualization
        h_lines, v_lines, debug_img = detect_grid_lines(grid_img, filename=metadata_row['Filename'])
        if not h_lines or not v_lines:
            return None

        # Calculate ROI area
        area_sq_microns, area_vis = calculate_roi_area(masked_img, h_lines, v_lines)
        if area_sq_microns is None:
            return None

        del grid_img
        gc.collect()

        # Save area visualization
        try:
            plt.figure(figsize=(10, 10))
            plt.imshow(area_vis)
            plt.title(f"ROI Area Measurement\nArea: {area_sq_microns/1e6:.2f} mm²")
            plt.axis('off')
            plt.savefig(
                str(vis_dir / f"{Path(metadata_row['Filename']).stem}_area_measurement.png"),
                bbox_inches='tight',
                dpi=250
            )
        finally:
            plt.close()
            del area_vis
            gc.collect()

        # Create and save tiles
        tile_paths = []
        tiles = create_tiles_from_masked(masked_img, h_lines, v_lines)

        if not tiles:
            logger.warning(f"No valid tiles found for {metadata_row['Filename']}")
            return None

        # Save each tile
        for tile_idx, tile in enumerate(tiles, 1):
            try:
                original_name = Path(metadata_row['Filename']).stem
                new_name = f"{original_name} - {tile_idx}.png"
                output_path = tiles_dir / new_name

                cv2.imwrite(str(output_path), tile)
                tile_paths.append(str(output_path.relative_to(base_path)))
            finally:
                del tile
                gc.collect()

        # Create visualization
        visualize_tile_process(masked_img, debug_img, tiles, metadata_row, vis_dir)
        del debug_img, masked_img
        gc.collect()

        return tile_paths, area_sq_microns

    except Exception as e:
        logger.error(f"Error processing {metadata_row['Filename']}: {str(e)}")
        return None
    finally:
        gc.collect()

def process_all_masked_images(drive_folder_path, stain_subset=None):
    """Process all masked images using metadata with improved memory management.

    Args:
        drive_folder_path: Path to the drive folder
        stain_subset: List of specific stains to process. If None, processes all stains.
    """
    base_path = None
    metadata_df = None

    try:
        # Initial aggressive memory cleanup before starting
        plt.close('all')
        for _ in range(10):
            gc.collect()

        if platform.system() != 'Windows':
            try:
                os.system('sync')
                os.system('echo 3 > /proc/sys/vm/drop_caches')
            except Exception as e:
                logger.warning(f"Could not perform initial memory cleanup: {e}")

        time.sleep(5)  # Give system time to clean up
        log_memory()  # Log initial memory state

        base_path = Path(drive_folder_path)
        metadata_path = base_path / 'metadata.csv'

        # Load metadata with error checking
        try:
            metadata_df = pd.read_csv(metadata_path)
            if metadata_df.empty:
                raise ValueError("Empty metadata file")
            logger.info(f"Found {len(metadata_df)} images")

            # Add new columns if they don't exist
            new_columns = ['TilePaths', 'TILEVisualizationPath', 'ROIArea_sq_microns']
            for col in new_columns:
                if col not in metadata_df.columns:
                    metadata_df[col] = None
        except Exception as e:
            logger.error(f"Error loading metadata: {e}")
            return None

        # Create directories
        tiles_dir = base_path / 'Tile-Images'
        vis_dir = base_path / 'TILEvisualizations'
        tiles_dir.mkdir(exist_ok=True)
        vis_dir.mkdir(exist_ok=True)

        # Get stain types based on subset
        all_stains = metadata_df['Staining'].unique().tolist()
        if stain_subset is not None:
            stain_types = [stain for stain in stain_subset if stain in all_stains]
        else:
            stain_types = all_stains

        logger.info(f"Processing staining types: {stain_types}")

        # Process each staining type
        for stain in stain_types:
            try:
                print(f"\nNow processing {stain} staining...")
                log_memory()

                # Create stain-specific directories
                stain_tiles_dir = tiles_dir / stain
                stain_vis_dir = vis_dir / stain
                stain_tiles_dir.mkdir(parents=True, exist_ok=True)
                stain_vis_dir.mkdir(parents=True, exist_ok=True)

                # Get stain-specific data efficiently
                stain_mask = metadata_df['Staining'] == stain
                stain_indices = metadata_df.index[stain_mask].tolist()

                # Clear mask to free memory
                del stain_mask
                gc.collect()

                # Process all images in staining group
                for idx in tqdm(stain_indices, desc=f"Processing {stain} images"):
                    try:
                        # Get row without copying entire dataframe
                        row = metadata_df.loc[idx]

                        # Check files exist
                        masked_path = base_path / row['MaskedPath']
                        grid_path = base_path / row['GridPath']

                        if not (masked_path.exists() and grid_path.exists()):
                            logger.warning(f"Missing files for {row['Filename']}")
                            continue

                        # Process single image
                        result = process_single_image(
                            masked_path,
                            grid_path,
                            row,
                            base_path,
                            stain_vis_dir,
                            stain_tiles_dir
                        )

                        if result is not None:
                            tile_paths, area_sq_microns = result

                            # Update metadata
                            metadata_df.at[idx, 'TilePaths'] = ';'.join(tile_paths)
                            metadata_df.at[idx, 'ROIArea_sq_microns'] = area_sq_microns

                            # Save visualization path
                            vis_filename = f"{Path(row['Filename']).stem}_grid_visualization.png"
                            vis_path = stain_vis_dir / vis_filename
                            metadata_df.at[idx, 'TILEVisualizationPath'] = str(vis_path.relative_to(base_path))

                            logger.info(f"Processed image with {len(tile_paths)} tiles, area: {area_sq_microns/1e6:.2f} mm²")

                            # Clear temporary variables
                            del tile_paths
                            del area_sq_microns
                            del vis_filename
                            del vis_path

                    except Exception as e:
                        logger.error(f"Error processing image {row['Filename']}: {e}")
                        continue
                    finally:
                        # Clean up resources
                        plt.close('all')
                        gc.collect()

                # Save progress after staining group
                metadata_df.to_csv(metadata_path, index=False)
                log_memory()

                # Aggressive cleanup after staining group
                plt.close('all')
                for _ in range(10):
                    gc.collect()

                # Force memory release on Unix systems
                if platform.system() != 'Windows':
                    try:
                        os.system('sync')
                        os.system('echo 3 > /proc/sys/vm/drop_caches')
                    except Exception as e:
                        logger.warning(f"Could not sync memory: {e}")

                # Add waiting time to allow system to clear memory
                time.sleep(5)

                # Log memory status after cleanup
                log_memory()
                if platform.system() != 'Windows':
                    try:
                        import resource
                        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        logger.info(f"Peak memory usage: {maxrss / 1024:.2f} MB")
                    except Exception as e:
                        logger.warning(f"Could not log peak memory: {e}")

            except Exception as e:
                logger.error(f"Error processing staining type {stain}: {e}")
                continue
            finally:
                # Additional cleanup after each stain type
                plt.close('all')
                gc.collect()

        return metadata_path

    except Exception as e:
        logger.error(f"Error in process_all_masked_images: {str(e)}")
        return None
    finally:
        # Final cleanup
        plt.close('all')
        if 'metadata_df' in locals():
            del metadata_df
        gc.collect()
        log_memory()

def main():
    """Main function to run the grid processing."""
    try:
        # Mount Google Drive
        try:
            drive.mount('/content/drive')
        except Exception as e:
            logger.info("Drive already mounted")

        # Get folder path
        folder_path = os.getenv('PROCESSED_FOLDER_PATH')
        if not folder_path:
            logger.error("Previous folder path not found! Please run the SVG processing script first.")
            return

        base_path = Path(folder_path)
        if not (base_path / 'metadata.csv').exists() or not (base_path / 'Masked-Results').exists():
            logger.error(f"Required files missing in {folder_path}")
            return

        logger.info(f"Using folder: {folder_path}")

        # Process images
        processed_results = process_all_masked_images(folder_path)

        if processed_results:
            logger.info("Processing completed successfully!")
        else:
            logger.error("Processing failed!")

    except Exception as e:
        logger.error(f"Error in main: {e}")
    finally:
        plt.close('all')
        gc.collect()

if __name__ == "__main__":
    main()




"""# **Step 3:** Staining dependent color segmentation"""


import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import cv2
from tqdm import tqdm
import os
import re
from pathlib import Path
from google.colab import drive
import logging
import gc
import psutil

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_memory():
    """Log current memory usage."""
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    logger.info(f"Memory usage: {memory_mb:.2f} MB")

def sanitize_filename(name):
    return re.sub(r'[^\w\-_]', '_', name)

def load_metadata(file_path):
    try:
        metadata_df = pd.read_csv(file_path)
        if metadata_df.empty:
            raise ValueError("The metadata file is empty.")
        logger.info(f"Successfully loaded metadata with {len(metadata_df)} rows")
        return metadata_df
    except Exception as e:
        logger.error(f"Error loading metadata: {e}")
        return None

def define_distinctive_color_groups():
    return {
        'HE': {
            'Nuclei': [
                (81, 44, 109),    # Dark purple
                (130, 82, 132),   # Medium purple
                (165, 127, 175)   # Light purple
            ],
            'Cytoplasm/Fibrosis/Muscle': [
                (136, 41, 73),    # Dark pink
                (209, 83, 145),   # Medium pink
                (239, 170, 216)   # Light pink
            ],
            'Other': [
                (210, 149, 191),  # Medium mauve
                (235, 140, 198),  # Bright pink
                (245, 235, 243)   # Very light pink
            ]
        },
        'Trichrome': {
            'Nuclei/Cytoplasm': [
                (106, 44, 60),    # Dark red
                (142, 59, 75),    # Medium red
                (209, 160, 172)   # Light pink
            ],
            'Fibrosis': [
                (102, 98, 114),   # Dark blue-gray
                (151, 131, 145),  # Medium gray
                (190, 200, 211)   # Light blue-gray
            ],
            'Muscle': [
                (115, 14, 15),    # Dark red
                (147, 49, 63),    # Medium red
                (180, 97, 111)    # Light red
            ],
            'Other': [
                (214, 193, 205),  # Medium pink
                (236, 234, 239),  # Very light gray
                (242, 233, 239)   # White-pink
            ]
        },
        'Movats': {
            'Nuclei/Elastin': [
                (20, 3, 10),      # Almost black
                (44, 20, 39),     # Dark purple
                (89, 44, 59)      # Medium purple-brown
            ],
            'Fibrosis': [
                (70, 30, 39),     # Dark brown
                (144, 95, 82),    # Medium brown
                (189, 168, 177)   # Light gray-pink
            ],
            'Muscle/Cytoplasm': [
                (57, 10, 19),     # Dark red
                (108, 27, 31),    # Medium red
                (147, 82, 99)     # Light red-pink
            ],
            'Other': [
                (161, 130, 140),  # Medium pink
                (213, 185, 191),  # Light pink
                (243, 237, 237)   # Almost white
            ]
        },
        'IHC': {
            'Nuclei': [
                (20, 15, 17),     # Almost black
                (129, 123, 142),  # Medium gray
                (214, 205, 212)   # Light gray
            ],
            'Target': [
                (72, 36, 16),     # Dark brown
                (166, 139, 125),  # Medium brown
                (234, 196, 170)   # Light brown
            ],
            'Other': [
                (191, 188, 190),  # Medium gray
                (229, 221, 220),  # Light gray
                (241, 235, 234)   # Almost white
            ]
        }
    }

def get_color_group(stain):
    predefined_groups = define_predefined_color_groups()
    ihc_stains = ['CD31', 'CD68', 'FSP1', 'Desmin', 'Laminin', 'Collagen']

    if stain in predefined_groups:
        return predefined_groups[stain]
    elif stain in ihc_stains:
        return predefined_groups['IHC']
    else:
        print(f"Warning: No predefined color group for {stain}. Using IHC colors.")
        return predefined_groups['IHC']

def segment_image(image, color_groups):
    """Segment image based on color groups."""
    try:
        pixels = image.reshape(-1, 3).astype(np.float64)
        distances = np.zeros((len(pixels), len(color_groups)))

        for i, colors in enumerate(color_groups.values()):
            colors_array = np.array(colors)
            distances[:, i] = np.min(np.linalg.norm(pixels[:, np.newaxis] - colors_array, axis=2), axis=1)
            del colors_array  # Free memory

        white_pixels = np.all(pixels > 240, axis=1)
        labels = np.zeros(len(pixels), dtype=int)
        non_white_mask = ~white_pixels
        labels[non_white_mask] = np.argmin(distances[non_white_mask], axis=1)
        labels[white_pixels] = -1

        del distances, pixels, white_pixels, non_white_mask  # Free memory
        result = labels.reshape(image.shape[:2])
        del labels  # Free memory
        return result

    except Exception as e:
        print(f"Error in image segmentation: {e}")
        return None

def create_visualization(image, segmented, white_mask, percentages, color_groups, stain, tile_path, show_percentages=False):
    """Create visualization of segmentation results."""
    try:
        n_colors = len(color_groups)
        fig, axes = plt.subplots(1, n_colors + 1, figsize=(5 * (n_colors + 1), 5))

        # Original image
        axes[0].imshow(image)
        axes[0].set_title("Original Tile")
        axes[0].axis('off')

        # Segmented regions
        for i, (name, _) in enumerate(color_groups.items()):
            segment = np.where(
                np.logical_and(segmented[..., np.newaxis] == i,
                             ~white_mask[..., np.newaxis]),
                image,
                [255, 255, 255]
            )
            axes[i + 1].imshow(segment.astype(np.uint8))
            title = f"{name}\n({percentages[name]:.1f}%)"
            axes[i + 1].set_title(title)
            axes[i + 1].axis('off')
            del segment  # Free memory

        plt.suptitle(f"Analysis of {os.path.basename(tile_path)}\n{stain} Staining", fontsize=10)
        plt.tight_layout()
        return fig

    except Exception as e:
        print(f"Error creating visualization: {e}")
        return None
    finally:
        plt.close('all')

def analyze_tile(tile_path, stain, color_groups, base_path, show_output=False):
    """Analyze a single tile and return percentages."""
    try:
        full_tile_path = base_path / tile_path
        if not full_tile_path.exists():
            print(f"Tile not found: {full_tile_path}")
            return None, None

        img = cv2.imread(str(full_tile_path))
        if img is None:
            print(f"Failed to read image: {full_tile_path}")
            return None, None

        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        del img  # Free memory

        segmented = segment_image(img_rgb, color_groups)
        if segmented is None:
            return None, None

        white_mask = np.all(img_rgb > 240, axis=2)
        total_valid_pixels = np.sum(~white_mask)

        if total_valid_pixels == 0:
            print(f"No valid pixels in tile: {tile_path}")
            return None, None

        # Calculate percentages
        percentages = {}
        for i, (name, _) in enumerate(color_groups.items()):
            segment_pixels = np.sum(np.logical_and(segmented == i, ~white_mask))
            percentage = (segment_pixels / total_valid_pixels) * 100
            percentages[name] = percentage

        # Create visualization
        fig = create_visualization(img_rgb, segmented, white_mask, percentages,
                                 color_groups, stain, tile_path, show_percentages=show_output)

        del img_rgb, segmented, white_mask  # Free memory
        gc.collect()  # Force garbage collection

        return fig, percentages

    except Exception as e:
        print(f"Error analyzing tile {tile_path}: {e}")
        return None, None

def display_color_palette(color_groups, stain, title):
    """Display the color palette for a stain type."""
    try:
        fig, ax = plt.subplots(figsize=(10, 2))
        for i, (name, colors) in enumerate(color_groups.items()):
            for j, color in enumerate(colors):
                ax.add_patch(plt.Rectangle((i + j/len(colors), 0),
                                        1/len(colors), 1,
                                        facecolor=np.array(color)/255))
            ax.text(i+0.5, -0.1, name,
                   ha='center', va='center', rotation=45)

        ax.set_xlim(0, len(color_groups))
        ax.set_ylim(-0.5, 1)
        ax.axis('off')
        plt.title(title)
        plt.tight_layout()
        plt.show()
    except Exception as e:
        print(f"Error displaying color palette: {e}")
    finally:
        plt.close('all')

def process_tiles_for_staining(metadata_df, stain, color_groups, base_path):
    """Process all tiles for a specific staining type."""
    print(f"\nProcessing {stain} stained tiles:")
    log_memory()

    # Create output directory in the base path with staining-specific subfolder
    output_dir = base_path / 'Staining-Analysis' / stain
    output_dir.mkdir(parents=True, exist_ok=True)

    # Display color palette first
    print(f"Displaying color palette for {stain} staining...")
    display_color_palette(color_groups, stain, f"Color Palette for {stain} Stain")

    # Get relevant rows and create a copy to avoid modifying original
    stain_rows = metadata_df[metadata_df['Staining'] == stain].copy()
    print(f"Found {len(stain_rows)} images with {stain} staining")

    # Initialize percentage and SD columns, and create a list to store tile data
    tile_data_list = []

    for segment_name in color_groups.keys():
        mean_col = f"{stain}_{sanitize_filename(segment_name)}_Percentage"
        sd_col = f"{stain}_{sanitize_filename(segment_name)}_SD"
        if mean_col not in metadata_df.columns:
            metadata_df[mean_col] = np.nan
            print(f"Created column: {mean_col}")
        if sd_col not in metadata_df.columns:
            metadata_df[sd_col] = np.nan
            print(f"Created column: {sd_col}")

    # Process each image
    for idx, row in tqdm(stain_rows.iterrows(), total=len(stain_rows), desc=f"Processing {stain} images"):
        try:
            if pd.isna(row['TilePaths']):
                logger.warning(f"No tiles found for image {row['Filename']}")
                continue

            tile_paths = row['TilePaths'].split(';')
            segment_percentages = {name: [] for name in color_groups.keys()}

            for tile_path in tile_paths:
                fig, percentages = analyze_tile(tile_path, stain, color_groups, base_path, show_output=False)

                if percentages:
                    # Store individual tile data
                    tile_data = {
                        'Animal': row['Animal'],
                        'Condition': row['Condition'],
                        'Week': row['Week'],
                        'Location': row['Location'],
                        'TilePath': tile_path
                    }
                    # Add percentages for each segment
                    for segment_name, percentage in percentages.items():
                        tile_data[f"{stain}_{sanitize_filename(segment_name)}_Percentage"] = percentage
                    tile_data_list.append(tile_data)

                    # Accumulate percentages for mean/SD calculation
                    for segment_name, percentage in percentages.items():
                        segment_percentages[segment_name].append(percentage)

                    # Save visualization
                    if fig:
                        try:
                            base_name = Path(tile_path).stem
                            vis_path = output_dir / f"{base_name}_analysis.png"
                            fig.savefig(str(vis_path), dpi=250, bbox_inches='tight')
                        finally:
                            plt.close(fig)
                            del fig

            # Calculate and store mean percentages and SDs
            for segment_name, percentage_list in segment_percentages.items():
                if percentage_list:
                    mean_col = f"{stain}_{sanitize_filename(segment_name)}_Percentage"
                    sd_col = f"{stain}_{sanitize_filename(segment_name)}_SD"
                    mean_percentage = np.mean(percentage_list)
                    sd_percentage = np.std(percentage_list, ddof=1) if len(percentage_list) > 1 else 0
                    metadata_df.at[idx, mean_col] = mean_percentage
                    metadata_df.at[idx, sd_col] = sd_percentage

            # Clean up after each image
            del segment_percentages
            gc.collect()

        except Exception as e:
            logger.error(f"Error processing image {row['Filename']}: {e}")
            continue

        # Periodically log memory usage
        if idx % 10 == 0:
            log_memory()

    # Save individual tile data
    tile_df = pd.DataFrame(tile_data_list)
    tile_data_path = base_path / f'{stain}_tile_data.csv'
    tile_df.to_csv(tile_data_path, index=False)
    print(f"Saved individual tile data to: {tile_data_path}")

    # Final cleanup
    plt.close('all')
    gc.collect()
    log_memory()

    return metadata_df, tile_df

def main():
    # Mount Google Drive if not already mounted
    try:
        drive.mount('/content/drive')
    except Exception as e:
        logger.info("Drive already mounted")

    # Get metadata path from the previously processed folder
    folder_path = os.getenv('PROCESSED_FOLDER_PATH')

    if not folder_path:
        logger.error("Previous folder path not found! Please run the SVG processing script first.")
        return

    base_path = Path(folder_path)
    required_files = [
        base_path / 'metadata.csv',
        base_path / 'Tile-Images'
    ]

    if not all(path.exists() for path in required_files):
        logger.error(f"Required files missing in {folder_path}. Please ensure the folder contains metadata.csv and Tiles directory.")
        return

    logger.info(f"Found required files and directories in: {folder_path}")

    # Load metadata
    metadata_path = base_path / 'metadata.csv'
    metadata_df = load_metadata(metadata_path)
    if metadata_df is None:
        return

    # Get stain types
    stain_types = metadata_df['Staining'].unique()
    logger.info(f"Detected stain types: {stain_types}")

    # Create a dictionary to store all tile DataFrames
    all_tile_data = {}

    try:
        # Process each stain type
        for stain in stain_types:
            logger.info(f"\nProcessing {stain} staining...")
            color_group = get_color_group(stain)

            # Process tiles and get both metadata and tile data
            metadata_df, tile_df = process_tiles_for_staining(metadata_df, stain, color_group, base_path)

            # Store tile data
            all_tile_data[stain] = tile_df

            # Save progress after each stain type
            metadata_df.to_csv(metadata_path, index=False)

            # Verify columns were created
            stain_cols = [col for col in metadata_df.columns
                         if col.startswith(f"{stain}_")
                         and col.endswith('_Percentage')]
            logger.info(f"Created columns: {stain_cols}")

            # Clean up after each stain type
            gc.collect()
            log_memory()

        # Final save and verification
        metadata_df.to_csv(metadata_path, index=False)
        logger.info("\nMetadata saved successfully")

        # Save combined tile data
        combined_tile_data = pd.concat(all_tile_data.values(), ignore_index=True)
        combined_tile_path = base_path / 'all_tile_data.csv'
        combined_tile_data.to_csv(combined_tile_path, index=False)
        logger.info(f"\nSaved combined tile data to: {combined_tile_path}")

        # Print summary of tile data
        for stain, tile_df in all_tile_data.items():
            logger.info(f"\n{stain} tile data summary:")
            logger.info(f"Number of tiles: {len(tile_df)}")
            logger.info(f"Number of animals: {tile_df['Animal'].nunique()}")
            logger.info(f"Number of conditions: {tile_df['Condition'].nunique()}")
            logger.info(f"Percentage columns: {[col for col in tile_df.columns if 'Percentage' in col]}")

        # Verify percentage columns in metadata
        percentage_cols = [col for col in metadata_df.columns
                          if col.endswith('_Percentage')]
        logger.info(f"\nTotal percentage columns in metadata: {len(percentage_cols)}")
        logger.info(f"Columns: {percentage_cols}")

    except Exception as e:
        logger.error(f"Error in main processing: {e}")
    finally:
        plt.close('all')
        gc.collect()
        log_memory()

    return base_path

if __name__ == "__main__":
    main()


"""Quantification using individual tile values and mixed effect model"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from scipy import stats
import statsmodels.api as sm
import statsmodels.formula.api as smf
from statsmodels.stats.multitest import multipletests
import os
from pathlib import Path
from google.colab import drive
import warnings
warnings.filterwarnings('ignore')
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def sanitize_filename(name):
    """Convert a string to a valid filename by replacing invalid characters."""
    return name.replace('/', '_').replace('\\', '_').replace(' ', '_')

def perform_statistical_analysis(data, staining, location, segment_name,
                               percentage_col, output_dir, safe_location, safe_segment):
    """Perform mixed effects statistical analysis for each week separately."""
    weeks = sorted(data['Week'].unique())
    results = []

    FONT_SIZE = {
        'title': 18,
        'axes_labels': 16,
        'tick_labels': 16,
        'legend': 14
    }

    print(f"\nPerforming statistical analysis for {staining} - {location} - {segment_name}")
    print(f"Number of weeks to analyze: {len(weeks)}")

    # Create comprehensive p-value matrix across all timepoints
    # First, get only the groups that actually have data
    all_groups = []
    for week in weeks:
        week_data = data[data['Week'] == week]
        conditions = sorted(week_data['Condition'].unique())
        for condition in conditions:
            if len(week_data[week_data['Condition'] == condition]) > 0:  # Check if there's actual data
                all_groups.append(f"{condition}_W{week}")

    comprehensive_matrix = pd.DataFrame(1.0, index=all_groups, columns=all_groups)

    # Add comprehensive analysis results to text output
    results.extend([
        "COMPREHENSIVE ANALYSIS ACROSS ALL TIME POINTS",
        "============================================",
        f"\nAnalyzing all combinations for {staining} - {location} - {segment_name}",
        f"Total number of groups: {len(all_groups)}",
        "Groups included in analysis:",
        ", ".join(all_groups),
        "\nPairwise Comparisons:"
    ])

    # Perform all pairwise comparisons
    from itertools import combinations
    pairs = list(combinations(all_groups, 2))

    for group1, group2 in pairs:
        cond1, week1 = group1.rsplit('_W', 1)
        cond2, week2 = group2.rsplit('_W', 1)

        values1 = data[(data['Week'] == int(week1)) &
                     (data['Condition'] == cond1)][percentage_col]
        values2 = data[(data['Week'] == int(week2)) &
                     (data['Condition'] == cond2)][percentage_col]

        if len(values1) > 0 and len(values2) > 0:  # Only compare if both groups have data
            t_stat, p_value = stats.ttest_ind(values1, values2, equal_var=False)
            comprehensive_matrix.loc[group1, group2] = p_value
            comprehensive_matrix.loc[group2, group1] = p_value
            results.append(f"{group1} vs {group2}: p-value = {p_value:.6f}")

    results.extend([
        "\nComprehensive P-value Matrix:",
        comprehensive_matrix.to_string(),
        "\nWEEK-BY-WEEK ANALYSIS",
        "====================="
    ])

    # Create comprehensive heatmap
    plt.figure(figsize=(15, 13))
    mask = np.triu(np.ones_like(comprehensive_matrix, dtype=bool), k=1)

    sns.heatmap(comprehensive_matrix.astype(float),
                mask=mask,
                annot=True,
                cmap='coolwarm_r',
                vmin=0,
                vmax=1,
                fmt='.6f',
                linewidths=0.5,
                square=True)

    plt.title(f'Comprehensive P-value Analysis\n{staining} {segment_name} ({location})',
             fontsize=FONT_SIZE['title'])
    plt.xticks(rotation=45, ha='right', fontsize=FONT_SIZE['tick_labels'])
    plt.yticks(rotation=0, fontsize=FONT_SIZE['tick_labels'])

    plt.tight_layout()

    # Save comprehensive heatmap
    filename_base = f'{staining}_{safe_location}_{safe_segment}'
    comp_heatmap_path = output_dir / f'{filename_base}_comprehensive_pvalues.png'
    plt.savefig(str(comp_heatmap_path), dpi=300, bbox_inches='tight')
    plt.savefig(str(output_dir / f'{filename_base}_comprehensive_pvalues.svg'),
                format='svg', bbox_inches='tight')
    plt.close()

    # Original week-by-week analysis
    for week in weeks:
        week_data = data[data['Week'] == week].copy()

        # Check if we have any data for this week
        if len(week_data) == 0:
            print(f"No data available for Week {week}")
            continue

        # Convert data types explicitly
        week_data[percentage_col] = pd.to_numeric(week_data[percentage_col], errors='coerce')
        week_data = week_data.dropna(subset=[percentage_col])

        # Get conditions that have data for this week
        available_conditions = week_data['Condition'].unique()
        print(f"\nAnalyzing Week {week}:")
        print(f"Number of samples: {len(week_data)}")
        print(f"Available conditions: {available_conditions}")

        if len(available_conditions) <= 1:
            print(f"Only one or no conditions available for week {week}, skipping statistical analysis")
            continue

        try:
            # Ensure correct data types
            week_data['Animal'] = week_data['Animal'].astype(str)
            week_data['Condition'] = week_data['Condition'].astype('category')

            # Fit mixed effects model
            model = smf.mixedlm(f"{percentage_col} ~ Condition", data=week_data, groups=week_data["Animal"])
            model_results = model.fit()

            print("\nModel Summary:")
            print(model_results.summary())

            # Perform pairwise comparisons only for available conditions
            from itertools import combinations
            pairs = list(combinations(available_conditions, 2))
            p_values = []

            print("\nPairwise Comparisons:")
            for pair in pairs:
                group1 = week_data[week_data['Condition'] == pair[0]][percentage_col]
                group2 = week_data[week_data['Condition'] == pair[1]][percentage_col]

                if len(group1) > 0 and len(group2) > 0:  # Only compare if both groups have data
                    t_stat, p_value = stats.ttest_ind(group1, group2, equal_var=False)
                    p_values.append(p_value)
                    print(f"{pair[0]} vs {pair[1]}: p-value = {p_value:.6f}")

            if p_values:  # Only create heatmap if we have comparisons
                # Adjust p-values for multiple comparisons
                reject, pvals_corrected, _, _ = multipletests(p_values, method='bonferroni')
                p_value_matrix = pd.DataFrame(1.0, index=available_conditions, columns=available_conditions)

                for idx, pair in enumerate(pairs):
                    p_value = pvals_corrected[idx]
                    p_value_matrix.loc[pair[0], pair[1]] = p_value
                    p_value_matrix.loc[pair[1], pair[0]] = p_value

                # Generate heatmap
                plt.figure(figsize=(8, 6))
                sns.heatmap(p_value_matrix.astype(float),
                           mask=np.triu(np.ones_like(p_value_matrix, dtype=bool), k=1),
                           annot=True, cmap='coolwarm_r',
                           vmin=0, vmax=1,
                           fmt='.6f',
                           linewidths=0.5,
                           square=True)

                plt.title(f'P-value Heatmap - {staining} {segment_name}\n({location}) Week {week}',
                         fontsize=FONT_SIZE['title'])
                plt.xticks(rotation=45, ha='right', fontsize=FONT_SIZE['tick_labels'])
                plt.yticks(fontsize=FONT_SIZE['tick_labels'])

                plt.tight_layout()

                # Save heatmap
                filename_base = f'{staining}_{safe_location}_{safe_segment}'
                heatmap_path = output_dir / f'{filename_base}_week{week}_pvalues.png'
                plt.savefig(str(heatmap_path), dpi=250, bbox_inches='tight')
                plt.close()

                # Save results
                results.extend([
                    f"\nWeek {week}:",
                    "Mixed Effects Model Results:",
                    str(model_results.summary()),
                    "\nPairwise t-tests with Bonferroni correction:",
                    str(p_value_matrix)
                ])

        except Exception as e:
            print(f"Error in statistical analysis for week {week}: {str(e)}")
            continue

    # Save statistical results
    if results:
        filename_base = f'{staining}_{safe_location}_{safe_segment}'
        results_path = output_dir / f'{filename_base}_stats.txt'
        with open(results_path, 'w') as f:
            f.write(f"Statistical Analysis for {staining} - {location} - {segment_name}\n")
            f.write("\n".join(results))


def create_staining_plots(metadata_df, staining, output_dir):
    plt.rcParams['figure.figsize'] = (15, 10)
    plt.rcParams['axes.grid'] = True
    plt.rcParams['grid.alpha'] = 0.3
    plt.rcParams['grid.linestyle'] = '--'

    STANDARD_COLORS = {
        'Sham': '#CC0000',
        'Ctrl': '#404040',
        'Native': '#002B5C',
        'Test': '#006B5C',
        'Treatment': '#457B9D'
    }

    available_markers = ['o', 's', '^', 'D', 'v', 'P', 'X', 'p', '*', 'h',
                        '+', 'x', '1', '2', '3', '4', '<', '>', 'H', 'd']

    tile_data = pd.read_csv(Path(os.getenv('PROCESSED_FOLDER_PATH')) / f'{staining}_tile_data.csv')

    stain_data = metadata_df[metadata_df['Staining'] == staining].copy()
    locations = sorted(stain_data['Location'].unique())
    all_weeks = sorted(stain_data['Week'].unique())
    all_conditions = sorted(stain_data['Condition'].unique())

    condition_colors = {cond: STANDARD_COLORS.get(cond, '#808080') for cond in all_conditions}
    marker_styles = {condition: available_markers[idx % len(available_markers)]
                    for idx, condition in enumerate(sorted(all_conditions))}

    percentage_columns = [col for col in tile_data.columns
                       if col.startswith(f"{staining}_") and col.endswith("_Percentage")]

    for location in locations:
        location_data = tile_data[tile_data['Location'] == location]

        for percentage_col in percentage_columns:
            segment_name = percentage_col.replace(f"{staining}_", "").replace("_Percentage", "")
            safe_location = sanitize_filename(location)
            safe_segment = sanitize_filename(segment_name)

            plt.figure(figsize=(15, 10))
            ax = plt.gca()

            plt.grid(True, linestyle='--', alpha=0.3, linewidth=1.5)

            valid_weeks = []
            week_positions = {}
            current_pos = 0

            for week in all_weeks:
                week_data = location_data[location_data['Week'] == week]
                if len(week_data) > 0:
                    valid_weeks.append(week)
                    week_positions[week] = current_pos
                    current_pos += 1

            condition_means = {condition: {} for condition in all_conditions}
            condition_positions = {condition: {} for condition in all_conditions}

            for week in valid_weeks:
                week_data = location_data[location_data['Week'] == week]
                conditions_in_week = sorted(week_data['Condition'].unique())
                n_conditions = len(conditions_in_week)

                for condition_idx, condition in enumerate(conditions_in_week):
                    condition_data = week_data[week_data['Condition'] == condition]
                    values = condition_data[percentage_col]

                    if len(values) > 0:
                        offset = 0.8 * (condition_idx - (n_conditions-1)/2) / n_conditions
                        pos = week_positions[week] + offset

                        q1, median, q3 = np.percentile(values, [25, 50, 75])
                        iqr = q3 - q1
                        whisker_low = max(values.min(), q1 - 1.5 * iqr)
                        whisker_high = min(values.max(), q3 + 1.5 * iqr)
                        mean = values.mean()

                        condition_means[condition][week] = mean
                        condition_positions[condition][week] = pos

                        base_color = condition_colors[condition]
                        rgba = plt.matplotlib.colors.to_rgba(base_color)
                        r, g, b, a = rgba
                        box_color = (min(1.0, r * 1.2), min(1.0, g * 1.2), min(1.0, b * 1.2), a)
                        marker_color = (max(0, r * 0.8), max(0, g * 0.8), max(0, b * 0.8), a)

                        # Add jitter to points
                        jitter = np.random.normal(0, 0.02, size=len(values))

                        # Plot individual points with circles
                        plt.scatter(pos + jitter, values,
                                  marker='o',
                                  color=marker_color,
                                  edgecolor='black',
                                  linewidth=1,
                                  alpha=0.8,
                                  s=20,
                                  zorder=3)

                        # Plot mean with condition-specific marker
                        plt.plot([pos], [mean],
                                marker=marker_styles[condition],
                                color=box_color,
                                markerfacecolor=marker_color,
                                markeredgecolor='black',
                                markeredgewidth=1,
                                markersize=12,
                                linewidth=2.0,
                                alpha=0.9,
                                zorder=4)

                        box = plt.Rectangle((pos - 0.1, q1), 0.2, q3 - q1,
                                         facecolor=box_color,
                                         edgecolor='black',
                                         linewidth=1,
                                         alpha=1,
                                         zorder=2)
                        ax.add_patch(box)

                        plt.hlines(median, pos - 0.1, pos + 0.1,
                                 colors='black',
                                 linewidth=2.0,
                                 zorder=3)

                        plt.vlines(pos, whisker_low, whisker_high,
                                 colors=box_color,
                                 linewidth=2.0,
                                 zorder=2)
                        plt.hlines([whisker_low, whisker_high],
                                 pos - 0.1, pos + 0.1,
                                 colors=box_color,
                                 linewidth=2.0,
                                 zorder=2)

            for condition in all_conditions:
                weeks_with_data = sorted(condition_means[condition].keys())
                for i in range(len(weeks_with_data) - 1):
                    current_week = weeks_with_data[i]
                    next_week = weeks_with_data[i + 1]

                    base_color = condition_colors[condition]
                    rgba = plt.matplotlib.colors.to_rgba(base_color)
                    r, g, b, a = rgba
                    line_color = (min(1.0, r * 1.2), min(1.0, g * 1.2), min(1.0, b * 1.2), a)

                    plt.plot([condition_positions[condition][current_week],
                            condition_positions[condition][next_week]],
                           [condition_means[condition][current_week],
                            condition_means[condition][next_week]],
                           color=line_color,
                           linewidth=2.0,
                           alpha=0.9,
                           zorder=1)

            plt.title(f'{segment_name} Analysis for {staining} Staining - {location}',
                     fontsize=30, fontweight='bold')
            plt.xlabel('Week', fontsize=28, fontweight='bold')
            plt.ylabel(f'{segment_name} Percentage', fontsize=28, fontweight='bold')

            plt.xlim(-0.5, len(valid_weeks) - 0.5)
            plt.xticks(range(len(valid_weeks)),
                      [f'Week {w}' for w in valid_weeks],
                      fontsize=24)
            plt.yticks(fontsize=24)

            handles = []
            for condition in all_conditions:
                if condition in set().union(*[
                    set(location_data[location_data['Week'] == w]['Condition'].unique())
                    for w in valid_weeks
                ]):
                    base_color = condition_colors[condition]
                    rgba = plt.matplotlib.colors.to_rgba(base_color)
                    r, g, b, a = rgba
                    box_color = (min(1.0, r * 1.2), min(1.0, g * 1.2), min(1.0, b * 1.2), a)
                    marker_color = (max(0, r * 0.8), max(0, g * 0.8), max(0, b * 0.8), a)

                    line = plt.Line2D([0], [0],
                                    color=box_color,
                                    marker=marker_styles[condition],
                                    markerfacecolor=marker_color,
                                    markeredgecolor='black',
                                    markeredgewidth=1,
                                    markersize=12,
                                    linewidth=2.0,
                                    label=condition)
                    handles.append(line)

            ax.legend(handles=handles,
                     title='Condition',
                     loc='upper right',
                     fontsize=22,
                     title_fontsize=24)

            plt.tight_layout()

            filename_base = f'{staining}_{safe_location}_{safe_segment}'
            plt.savefig(output_dir / f'{filename_base}_analysis.png',
                       dpi=300, bbox_inches='tight')
            plt.savefig(output_dir / f'{filename_base}_analysis.svg',
                       format='svg', bbox_inches='tight')
            plt.close()

            perform_statistical_analysis(location_data, staining, location,
                                      segment_name, percentage_col, output_dir,
                                      safe_location, safe_segment)

def process_tile_data(metadata_df):

    if len(metadata_df) > 0:
        print("\nUnique values in key columns:")
        print(f"Staining types: {metadata_df['Staining'].unique()}")
        print(f"Conditions: {metadata_df['Condition'].unique()}")
        print(f"Weeks: {metadata_df['Week'].unique()}")

        percentage_cols = [col for col in metadata_df.columns if 'Percentage' in col]
        print(f"\nPercentage columns found: {percentage_cols}")

    metadata_df['Week_Group'] = 'Week ' + metadata_df['Week'].astype(str)

    return metadata_df

def create_area_plots(metadata_df, output_dir):
    if 'ROIArea_sq_microns' not in metadata_df.columns:
        logger.error("ROIArea_sq_microns column not found in metadata")
        return

    plt.rcParams['figure.figsize'] = (15, 10)
    plt.rcParams['axes.grid'] = True
    plt.rcParams['grid.alpha'] = 0.3
    plt.rcParams['grid.linestyle'] = '--'

    STANDARD_COLORS = {
        'Sham': '#CC0000',
        'Ctrl': '#404040',
        'Native': '#002B5C',
        'Test': '#006B5C'
    }

    available_markers = ['o', 's', '^', 'D', 'v', 'P', 'X', 'p', '*', 'h',
                        '+', 'x', '1', '2', '3', '4', '<', '>', 'H', 'd']

    stain_data = metadata_df[metadata_df['Staining'] == 'HE'].copy()
    if len(stain_data) == 0:
        logger.warning("No HE staining data found")
        return

    stain_data = stain_data[stain_data['Condition'] != 'Native']
    staining_dir = output_dir / 'HE'
    staining_dir.mkdir(parents=True, exist_ok=True)
    stain_data['Area_mm2'] = stain_data['ROIArea_sq_microns'] / 1e6

    conditions = sorted(stain_data['Condition'].unique())
    weeks = sorted(stain_data['Week'].unique())
    condition_colors = {cond: STANDARD_COLORS.get(cond, '#808080') for cond in conditions}
    marker_styles = {condition: available_markers[idx % len(available_markers)]
                    for idx, condition in enumerate(sorted(conditions))}

    plt.figure(figsize=(15, 10))
    ax = plt.gca()
    plt.grid(True, linestyle='--', alpha=0.3, linewidth=1.5)

    valid_weeks = []
    week_positions = {}
    current_pos = 0

    for week in weeks:
        week_data = stain_data[stain_data['Week'] == week]
        if len(week_data) > 0:
            valid_weeks.append(week)
            week_positions[week] = current_pos
            current_pos += 1

    for week in valid_weeks:
        week_data = stain_data[stain_data['Week'] == week]
        conditions_in_week = sorted(week_data['Condition'].unique())
        n_conditions = len(conditions_in_week)

        for condition_idx, condition in enumerate(conditions_in_week):
            condition_data = week_data[week_data['Condition'] == condition]['Area_mm2']

            if len(condition_data) > 0:
                offset = 0.8 * (condition_idx - (n_conditions-1)/2) / n_conditions
                pos = week_positions[week] + offset

                q1, median, q3 = np.percentile(condition_data, [25, 50, 75])
                iqr = q3 - q1
                whisker_low = max(condition_data.min(), q1 - 1.5 * iqr)
                whisker_high = min(condition_data.max(), q3 + 1.5 * iqr)
                mean = condition_data.mean()

                base_color = condition_colors[condition]
                rgba = plt.matplotlib.colors.to_rgba(base_color)
                r, g, b, a = rgba
                box_color = (min(1.0, r * 1.2), min(1.0, g * 1.2), min(1.0, b * 1.2), a)
                marker_color = (max(0, r * 0.8), max(0, g * 0.8), max(0, b * 0.8), a)

                # Plot individual points with circles
                jitter = np.random.normal(0, 0.02, size=len(condition_data))
                plt.scatter(pos + jitter, condition_data,
                          marker='o',
                          color=marker_color,
                          edgecolor='black',
                          linewidth=1,
                          alpha=0.8,
                          s=50,
                          zorder=3)

                # Plot mean with condition-specific marker
                plt.plot([pos], [mean],
                        marker=marker_styles[condition],
                        color=box_color,
                        markerfacecolor=marker_color,
                        markeredgecolor='black',
                        markeredgewidth=1,
                        markersize=12,
                        linewidth=2.0,
                        alpha=0.9,
                        zorder=4)

                box = plt.Rectangle((pos - 0.1, q1), 0.2, q3 - q1,
                                 facecolor=box_color,
                                 edgecolor='black',
                                 linewidth=1,
                                 alpha=1.0,
                                 zorder=2)
                ax.add_patch(box)

                plt.hlines(median, pos - 0.1, pos + 0.1,
                         colors='black',
                         linewidth=2.0,
                         zorder=3)

                plt.vlines(pos, whisker_low, whisker_high,
                         colors=box_color,
                         linewidth=2.0,
                         zorder=2)
                plt.hlines([whisker_low, whisker_high],
                         pos - 0.1, pos + 0.1,
                         colors=box_color,
                         linewidth=2.0,
                         zorder=2)

                if week != valid_weeks[-1]:
                    next_week_data = stain_data[
                        (stain_data['Week'] == valid_weeks[valid_weeks.index(week) + 1]) &
                        (stain_data['Condition'] == condition)
                    ]['Area_mm2']
                    if len(next_week_data) > 0:
                        next_pos = week_positions[valid_weeks[valid_weeks.index(week) + 1]] + offset
                        next_mean = next_week_data.mean()
                        plt.plot([pos, next_pos], [mean, next_mean],
                               color=box_color,
                               linewidth=2.0,
                               alpha=0.9,
                               zorder=1)

    locations = sorted(stain_data['Location'].unique())
    location_info = f" ({', '.join(locations)})" if len(locations) > 1 else ""

    plt.title(f'ROI Area Analysis for HE Staining{location_info}',
             fontsize=30, fontweight='bold')
    plt.xlabel('Week', fontsize=28, fontweight='bold')
    plt.ylabel('Area (mm²)', fontsize=28, fontweight='bold')

    plt.xlim(-0.5, len(valid_weeks) - 0.5)
    plt.xticks(range(len(valid_weeks)),
              [f'Week {w}' for w in valid_weeks],
              fontsize=24)
    plt.yticks(fontsize=24)

    handles = []
    for condition in conditions:
        if condition in set().union(*[
            set(stain_data[stain_data['Week'] == w]['Condition'].unique())
            for w in valid_weeks
        ]):
            base_color = condition_colors[condition]
            rgba = plt.matplotlib.colors.to_rgba(base_color)
            r, g, b, a = rgba
            box_color = (min(1.0, r * 1.2), min(1.0, g * 1.2), min(1.0, b * 1.2), a)
            marker_color = (max(0, r * 0.8), max(0, g * 0.8), max(0, b * 0.8), a)

            line = plt.Line2D([0], [0],
                            color=box_color,
                            marker=marker_styles[condition],
                            markerfacecolor=marker_color,
                            markeredgecolor='black',
                            markeredgewidth=1,
                            markersize=12,
                            linewidth=2.0,
                            label=condition)
            handles.append(line)

    ax.legend(handles=handles,
            title='Condition',
            loc='upper right',
            fontsize=22,
            title_fontsize=24)

    plt.tight_layout()

    plt.savefig(staining_dir / 'HE_area_analysis.png',
               dpi=300, bbox_inches='tight')
    plt.savefig(staining_dir / 'HE_area_analysis.svg',
               format='svg', bbox_inches='tight')
    plt.close()

    perform_statistical_analysis(stain_data, 'HE', "Combined Locations", "Area",
                              'Area_mm2', staining_dir,
                              "Combined", "Area")


def main():
    # Skip mounting if already mounted
    try:
        drive.mount('/content/drive')
    except Exception as e:
        logger.info("Drive already mounted")

    # Get metadata path from the previously processed folder
    folder_path = os.getenv('PROCESSED_FOLDER_PATH')

    if not folder_path:
        logger.error("Previous folder path not found! Please run the SVG processing script first.")
        return

    base_path = Path(folder_path)
    metadata_file = base_path / 'metadata.csv'

    if not metadata_file.exists():
        logger.error(f"Metadata file not found in: {folder_path}")
        return

    logger.info(f"Found metadata.csv in: {folder_path}")

    # Create output directory in the base folder
    output_dir = base_path / 'Quantification (Tile-Level)'
    output_dir.mkdir(exist_ok=True)

    logger.info("Loading metadata...")
    metadata_path = base_path / 'metadata.csv'
    metadata_df = pd.read_csv(metadata_path)

    logger.info("\nMetadata Overview:")
    logger.info(f"Total rows: {len(metadata_df)}")
    logger.info(f"Columns: {metadata_df.columns.tolist()}")

    logger.info("\nChecking for required columns...")
    required_columns = ['TilePaths', 'Staining', 'Condition', 'Week', 'Location']
    missing_columns = [col for col in required_columns if col not in metadata_df.columns]
    if missing_columns:
        logger.warning(f"Missing required columns: {missing_columns}")

    logger.info("\nCreating area visualizations...")
    create_area_plots(metadata_df, output_dir)

    logger.info("\nProcessing tile data...")
    tile_df = process_tile_data(metadata_df)

    if len(tile_df) == 0:
        logger.error("No tile data was processed. Check if TilePaths column contains valid data.")
        return

    logger.info("\nCreating plots and performing statistical analysis...")
    for staining in tile_df['Staining'].unique():
        logger.info(f"\nProcessing {staining} staining...")

        staining_cols = [col for col in tile_df.columns
                         if col.startswith(f"{staining}_") and col.endswith("_Percentage")]
        if not staining_cols:
            logger.warning(f"No percentage columns found for {staining} staining")
            continue

        # Create staining type subfolder
        staining_dir = output_dir / sanitize_filename(staining)
        staining_dir.mkdir(exist_ok=True)

        create_staining_plots(tile_df, staining, staining_dir)

    logger.info(f"\nAnalysis complete. Results saved in {output_dir}")

    return output_dir

if __name__ == "__main__":
    main()




"""# **Step 4:** Indices definition and visualization"""


import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
from google.colab import drive
import os
import logging
import numpy as np
from sklearn.linear_model import LinearRegression

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_marker_styles(conditions):
    """Dynamically assign marker styles based on conditions in data."""
    available_markers = ['o', 's', '^', 'D', 'v', 'P', 'X', 'p', '*', 'h',
                         '+', 'x', '1', '2', '3', '4', '<', '>', 'H', 'd']
    marker_styles = {}
    for idx, condition in enumerate(sorted(conditions)):
        marker_styles[condition] = available_markers[idx % len(available_markers)]
    return marker_styles


def get_marker_colors(markers):
    """Dynamically assign colors based on markers in data."""
    fixed_condition_colors = {
        'Sham': '#E63946',  # Red
        'Ctrl': '#808080',  # Grey
        'Native': '#1D3557',  # Blue
        'Test': '#2A9D8F'    # Green
    }
    additional_colors = ['#457B9D', '#E9C46A', '#F4A261', '#9B2226',
                         '#005F73', '#AE2012', '#3D405B', '#2B2D42',
                         '#8D99AE', '#EF233C', '#4A4E69', '#9A8C98']
    marker_colors = {}
    additional_idx = 0
    for idx, marker in enumerate(sorted(markers)):
        if marker in fixed_condition_colors:
            marker_colors[marker] = fixed_condition_colors[marker]
        else:
            marker_colors[marker] = additional_colors[additional_idx % len(additional_colors)]
            additional_idx += 1
    return marker_colors

def create_tpi_tables(he_means, marker_means_dict, nuclei_col, output_dir):
    """Create detailed tables of the data used in TPI calculations."""
    output_file = output_dir / 'analysis_summary.txt'
    with open(output_file, 'w') as summary_file:
        # 1. Create HE Nuclei percentage table
        he_table = he_means.pivot_table(
            values=nuclei_col,
            index=['Location', 'Week'],
            columns='Condition',
            aggfunc='mean'
        ).round(2)
        summary_text = "\nTPI Analysis Summary Report\n"
        summary_text += "==========================\n\n"
        summary_text += "1. HE Nuclei Percentages\n"
        summary_text += "-----------------------\n"
        summary_text += he_table.to_string() + "\n\n"
        print(summary_text)
        summary_file.write(summary_text)

        # 2. Process marker data
        summary_text = "2. IHC Target Percentages\n"
        summary_text += "-----------------------\n"
        print(summary_text)
        summary_file.write(summary_text)
        for marker, data in marker_means_dict.items():
            marker_table = data['means'].pivot_table(
                values=data['target_col'],
                index=['Location', 'Week'],
                columns='Condition',
                aggfunc='mean'
            ).round(2)
            marker_text = f"\n{marker} Target Percentages:\n"
            marker_text += marker_table.to_string() + "\n\n"
            print(marker_text)
            summary_file.write(marker_text)

        # 3. Create TPI tables
        summary_text = "3. Target Prevalence Index (TPI) Values\n"
        summary_text += "------------------------------------\n"
        print(summary_text)
        summary_file.write(summary_text)
        for marker, data in marker_means_dict.items():
            tpi_data = []
            for location in data['means']['Location'].unique():
                for condition in data['means']['Condition'].unique():
                    marker_subset = data['means'][
                        (data['means']['Location'] == location) &
                        (data['means']['Condition'] == condition)
                    ]
                    he_subset = he_means[
                        (he_means['Location'] == location) &
                        (he_means['Condition'] == condition)
                    ]
                    if not marker_subset.empty and not he_subset.empty:
                        for week in marker_subset['Week'].unique():
                            week_marker = marker_subset[marker_subset['Week'] == week][data['target_col']]
                            week_he = he_subset[he_subset['Week'] == week][nuclei_col]
                            if not week_marker.empty and not week_he.empty:
                                week_tpis = []
                                for m in week_marker:
                                    for h in week_he:
                                        if h != 0:  # Avoid division by zero
                                            week_tpis.append(m / h)
                                if week_tpis:
                                    tpi_data.append({
                                        'Location': location,
                                        'Condition': condition,
                                        'Week': week,
                                        'TPI': np.mean(week_tpis),
                                        'TPI_SD': np.std(week_tpis, ddof=1) if len(week_tpis) > 1 else 0
                                    })
            if tpi_data:
                tpi_df = pd.DataFrame(tpi_data)
                tpi_table = tpi_df.pivot_table(
                    values=['TPI', 'TPI_SD'],
                    index=['Location', 'Week'],
                    columns='Condition',
                    aggfunc={'TPI': 'mean', 'TPI_SD': 'mean'}
                ).round(3)
                tpi_text = f"\n{marker} TPI Values:\n"
                tpi_text += tpi_table.to_string() + "\n\n"
                print(tpi_text)
                summary_file.write(tpi_text)

                # 4. Add statistics
                stats_text = "4. Statistical Summary\n"
                stats_text += "-------------------\n"
                print(stats_text)
                summary_file.write(stats_text)
                stats_data = {
                    'Max TPI': tpi_df['TPI'].max(),
                    'Min TPI': tpi_df['TPI'].min(),
                    'Mean TPI': tpi_df['TPI'].mean(),
                    'Median TPI': tpi_df['TPI'].median(),
                    'Overall Std Dev': tpi_df['TPI'].std(),
                    'Average Within-Week Std Dev': tpi_df['TPI_SD'].mean()
                }
                for stat, value in stats_data.items():
                    stat_line = f"{stat}: {value:.3f}\n"
                    print(stat_line)
                    summary_file.write(stat_line)
                print("\n")
                summary_file.write("\n")

def create_marker_plot(marker_data, conditions, unique_weeks, marker, condition_colors, marker_styles, ax):
    """
    Create an improved plot for a single marker with distinct colors for boxes and markers.
    Individual lines connect consecutive boxes.
    """
    # Define fixed colors for known conditions
    base_colors = {
        'Sham': '#CC0000',    # Base red
        'Ctrl': '#404040',    # Base grey
        'Native': '#002B5C',  # Base blue
        'Test': '#006B5C'     # Base green
    }

    # Fallback colors for other conditions
    fallback_colors = ['#2B4B7E', '#B35900', '#8B0000', '#004D40']

    # Create color mappings section of the function:
    box_colors = {}
    marker_colors = {}
    fallback_idx = 0

    for condition in sorted(conditions):
        if condition in base_colors:
            base_color = base_colors[condition]
        else:
            base_color = fallback_colors[fallback_idx % len(fallback_colors)]
            fallback_idx += 1

        # Convert to RGB for manipulation
        rgba = plt.matplotlib.colors.to_rgba(base_color)
        r, g, b, a = rgba

        # Make boxes lighter by reducing color intensity less (multiply by 1.2 and cap at 1.0)
        box_rgba = (min(1.0, r * 1.2), min(1.0, g * 1.2), min(1.0, b * 1.2), a)
        box_colors[condition] = box_rgba

        # Create darker version for markers (20% darker than base)
        darker_rgba = (max(0, r * 0.8), max(0, g * 0.8), max(0, b * 0.8), a)
        marker_colors[condition] = darker_rgba

    # Plot settings
    marker_size = 50
    line_width = 2.0
    box_width = 0.2
    edge_width = 1

    # Create week position mapping
    week_positions = {week: i for i, week in enumerate(unique_weeks)}
    n_conditions = len(conditions)

    # Plot for each condition
    for condition_idx, condition in enumerate(sorted(conditions)):
        # Calculate offset for this condition
        offset = 0.8 * (condition_idx - (n_conditions-1)/2) / n_conditions
        condition_data = marker_data[marker_data['Condition'] == condition].copy()

        if condition_data.empty:
            continue

        # Add position column for plotting
        condition_data['plot_position'] = condition_data['Week'].map(week_positions)

        # 1. Plot scatter points
        ax.scatter(
            [week_positions[week] + offset for week in condition_data['Week']],
            condition_data['TPI'],
            color=marker_colors[condition],
            edgecolor='black',
            linewidth=edge_width,
            alpha=0.8,
            s=marker_size,
            zorder=3
        )

        # 2. Plot boxes for each week
        means_data = []
        positions = []

        for week in unique_weeks:
            week_data = condition_data[condition_data['Week'] == week]['TPI']
            if not week_data.empty:
                current_mean = week_data.mean()
                means_data.append(current_mean)
                positions.append(week_positions[week] + offset)

                ax.boxplot(
                    [week_data],
                    positions=[week_positions[week] + offset],
                    widths=box_width,
                    patch_artist=True,
                    medianprops={'color': 'black', 'linewidth': line_width},
                    boxprops={
                        'facecolor': box_colors[condition],
                        'alpha': 1.0,
                        'linewidth': line_width,
                        'edgecolor': 'black'
                    },
                    whiskerprops={'color': 'black', 'linewidth': line_width},
                    capprops={'color': 'black', 'linewidth': line_width},
                    flierprops={
                        'marker': 'o',
                        'markerfacecolor': box_colors[condition],
                        'markeredgecolor': 'black',
                        'markersize': 4,
                        'alpha': 1.0
                    },
                    zorder=2,
                    showfliers=False
                )

        # 3. Plot individual lines between consecutive means
        if len(means_data) > 1:
            for i in range(len(means_data) - 1):
                # Plot individual line segments
                ax.plot(
                    [positions[i], positions[i+1]],
                    [means_data[i], means_data[i+1]],
                    color=box_colors[condition],
                    linewidth=line_width,
                    alpha=0.9,
                    zorder=4
                )

                # Plot markers at each point
                ax.plot(
                    positions[i],
                    means_data[i],
                    color=box_colors[condition],
                    marker=marker_styles[condition],
                    markerfacecolor=marker_colors[condition],
                    markeredgecolor='black',
                    markeredgewidth=edge_width,
                    markersize=12,
                    zorder=5,
                    label=condition if i == 0 else ""  # Only add label for first point
                )

            # Plot the last marker separately
            ax.plot(
                positions[-1],
                means_data[-1],
                color=box_colors[condition],
                marker=marker_styles[condition],
                markerfacecolor=marker_colors[condition],
                markeredgecolor='black',
                markeredgewidth=edge_width,
                markersize=12,
                zorder=5
            )

    # Style the plot
    ax.set_xlabel('Week', fontsize=28, fontweight='bold')
    ax.set_ylabel('Target Prevalence Index (TPI)', fontsize=28, fontweight='bold')
    ax.set_title(f'{marker} Prevalence Over Time', fontsize=30, fontweight='bold')
    ax.grid(True, linestyle='--', alpha=0.4, linewidth=1.5)

    # Set axis properties
    ax.set_xticks(range(len(unique_weeks)))
    ax.set_xticklabels([f'Week {w}' for w in unique_weeks], fontsize=24)
    ax.tick_params(axis='y', labelsize=20)
    ax.tick_params(width=2)

    # Add legend
    ax.legend(
        title='Condition',
        loc='upper right',
        fontsize=22,
        title_fontsize=24
    )

    return ax

def create_tpi_plots(metadata_df, output_dir):
    """Create Target Prevalence Index (TPI) plots from metadata."""
    plt.rcParams['figure.figsize'] = (15, 10)
    plt.rcParams['axes.grid'] = True
    plt.rcParams['grid.alpha'] = 0.3
    plt.rcParams['grid.linestyle'] = '--'

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ihc_stains = ['Laminin', 'MHC', 'Collagen', 'Actinin', 'CD31', 'Acetylc',
                  'Tubulin', 'CD68', 'FSP1', 'Desmin']

    tile_df = metadata_df.copy()
    staining_types = sorted(tile_df['Staining'].unique())
    ihc_markers = sorted([s for s in staining_types if s in ihc_stains])
    conditions = sorted(tile_df['Condition'].unique())
    unique_weeks = sorted(tile_df['Week'].unique())

    logger.info(f"Found IHC markers to analyze: {ihc_markers}")
    logger.info(f"Found conditions: {conditions}")

    marker_styles = get_marker_styles(conditions)
    marker_colors = get_marker_colors(ihc_markers)

    for condition in conditions:
        if condition not in marker_styles:
            marker_styles[condition] = 'o'

    for marker in ihc_markers:
        if marker not in marker_colors:
            marker_colors[marker] = '#808080'

    he_data = tile_df[tile_df['Staining'] == 'HE'].copy()
    nuclei_col = [col for col in he_data.columns if col.startswith('HE_') and 'Nuclei' in col and col.endswith('_Percentage')]

    if not nuclei_col:
        logger.error("No HE Nuclei percentage column found!")
        return
    nuclei_col = nuclei_col[0]

    he_means = he_data.groupby(['Condition', 'Week', 'Location'])[nuclei_col].mean().reset_index()
    marker_means_dict = {}

    for marker in ihc_markers:
        marker_data = tile_df[tile_df['Staining'] == marker].copy()
        if len(marker_data) == 0:
            continue

        target_col = [col for col in marker_data.columns
                     if col.startswith(f'{marker}_Target')
                     and col.endswith('_Percentage')]

        if not target_col:
            logger.warning(f"No target percentage column found for {marker}")
            continue

        target_col = target_col[0]

        # Calculate regression-based TPI values
        tpi_data = []
        for condition in conditions:
            for week in unique_weeks:
                condition_data = marker_data[
                    (marker_data['Condition'] == condition) &
                    (marker_data['Week'] == week)
                ]

                matching_he = he_data[
                    (he_data['Condition'] == condition) &
                    (he_data['Week'] == week)
                ]

                if not condition_data.empty and not matching_he.empty:
                    # Perform regression for each location
                    for location in condition_data['Location'].unique():
                        loc_marker = condition_data[condition_data['Location'] == location][target_col].values
                        loc_he = matching_he[matching_he['Location'] == location][nuclei_col].values

                        if len(loc_marker) > 0 and len(loc_he) > 0:
                            # For each condition/week/location combination
                            X = loc_he.reshape(-1, 1)  # Independent variable (nuclei)
                            y = loc_marker            # Dependent variable (marker)
                            reg = LinearRegression()
                            reg.fit(X, y)
                            residuals = y - reg.predict(X)

                            # Append each individual point
                            for i in range(len(residuals)):
                                tpi_data.append({
                                    'Location': location,
                                    'Condition': condition,
                                    'Week': week,
                                    'TPI': residuals[i] + np.mean(y)  # Center around original mean
                                })

        if tpi_data:
            tpi_df = pd.DataFrame(tpi_data)
            marker_means = marker_data.groupby(['Location', 'Week', 'Condition'])[target_col].mean().reset_index()
            marker_means_dict[marker] = {
                'means': marker_means,
                'target_col': target_col,
                'tpi_data': tpi_df
            }
            perform_weighted_tpi_statistical_analysis(tpi_df, marker_data, target_col, marker, output_dir)

            plt.figure(figsize=(15, 10))
            ax = plt.gca()

            condition_colors = {
                condition: plt.cm.Set2(i/len(conditions))
                for i, condition in enumerate(sorted(conditions))
            }

            create_marker_plot(tpi_df, conditions, unique_weeks, marker,
                               condition_colors, marker_styles, ax)

            plt.tight_layout()
            plt.savefig(output_dir / f'target_prevalence_index_{marker}.png',
                       dpi=300, bbox_inches='tight')
            plt.savefig(output_dir / f'target_prevalence_index_{marker}.svg',
                       format='svg', bbox_inches='tight')
            plt.close()

    # Create combined plot if needed
    if len(marker_means_dict) > 0:
        plt.figure(figsize=(15, 10))
        ax = plt.gca()
        week_positions = {week: i for i, week in enumerate(unique_weeks)}

        for marker, data in marker_means_dict.items():
            tpi_df = data['tpi_data']

            for condition in conditions:
                condition_data = tpi_df[tpi_df['Condition'] == condition]
                if len(condition_data) > 0:
                    means = []
                    plot_positions = []

                    n_conditions = len(conditions)
                    condition_idx = sorted(conditions).index(condition)
                    offset = 0.4 * (condition_idx - (n_conditions-1)/2) / n_conditions

                    ax.scatter(
                        [week_positions[week] + offset for week in condition_data['Week']],
                        condition_data['TPI'],
                        color=marker_colors[marker],
                        alpha=0.4,
                        s=20,
                        zorder=3
                    )

                    for week in unique_weeks:
                        week_data = condition_data[condition_data['Week'] == week]['TPI']
                        if not week_data.empty:
                            means.append(week_data.mean())
                            plot_positions.append(week_positions[week])

                            ax.boxplot(
                                [week_data],
                                positions=[week_positions[week] + offset],
                                widths=0.2,
                                patch_artist=True,
                                medianprops=dict(color='black'),
                                boxprops=dict(facecolor=marker_colors[marker], alpha=0.3),
                                whiskerprops=dict(color=marker_colors[marker]),
                                capprops=dict(color=marker_colors[marker]),
                                flierprops=dict(marker='o', markerfacecolor=marker_colors[marker],
                                                markersize=4, alpha=0.5),
                                zorder=2,
                                showfliers=False
                            )

                    if means:
                        plt.plot([p + offset for p in plot_positions], means,
                                 color=marker_colors[marker],
                                 marker=marker_styles[condition],
                                 markersize=8,
                                 linewidth=1.5,
                                 alpha=0.7,
                                 label=f'{marker} - {condition}',
                                 zorder=4)

        plt.grid(True, linestyle='--', alpha=0.3)
        plt.xlabel('Week', fontsize=16)
        plt.ylabel('Target Prevalence Index (TPI)', fontsize=16)
        plt.title('Marker Prevalence Over Time', fontsize=18)
        plt.xticks(range(len(unique_weeks)), [f'Week {w}' for w in unique_weeks])

        handles, labels = plt.gca().get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        plt.legend(by_label.values(), by_label.keys(),
                   bbox_to_anchor=(1.05, 1),
                   loc='upper left',
                   fontsize=14,
                   title='Marker - Condition')

        plt.tight_layout()
        plt.savefig(output_dir / 'target_prevalence_index.png',
                   dpi=300, bbox_inches='tight')
        plt.savefig(output_dir / 'target_prevalence_index.svg',
                   format='svg', bbox_inches='tight')
        plt.close()

    create_tpi_tables(he_means, marker_means_dict, nuclei_col, output_dir)

    return output_dir

def perform_weighted_tpi_statistical_analysis(tpi_df, marker_data, target_col, marker, output_dir):
    """
    Performs statistical analysis using weighted t-tests with weights from original measurement SDs.
    """
    import statsmodels.api as sm
    from scipy import stats
    from statsmodels.stats.multitest import multipletests
    import numpy as np
    import seaborn as sns
    import matplotlib.pyplot as plt

    FONT_SIZE = {
        'title': 18,
        'axes_labels': 16,
        'tick_labels': 16,
        'legend': 14
    }

    weeks = sorted(tpi_df['Week'].unique())
    results = []

    print(f"\nPerforming TPI statistical analysis for {marker}")
    print(f"Number of weeks to analyze: {len(weeks)}")

    # Create comprehensive p-value matrix across all timepoints
    all_groups = []
    for week in weeks:
        week_data = tpi_df[tpi_df['Week'] == week]
        conditions = sorted(week_data['Condition'].unique())
        for condition in conditions:
            if len(week_data[week_data['Condition'] == condition]) > 0:
                all_groups.append(f"{condition}_W{week}")

    comprehensive_matrix = pd.DataFrame(1.0, index=all_groups, columns=all_groups)

    # Get SD column names
    marker_sd_col = target_col.replace('Percentage', 'SD')

    results.extend([
        "COMPREHENSIVE ANALYSIS ACROSS ALL TIME POINTS",
        "============================================",
        f"\nAnalyzing all combinations for {marker} TPI",
        f"Total number of groups: {len(all_groups)}",
        "Groups included in analysis:",
        ", ".join(all_groups),
        "\nPairwise Comparisons:"
    ])

    from itertools import combinations
    pairs = list(combinations(all_groups, 2))

    for group1, group2 in pairs:
        cond1, week1 = group1.rsplit('_W', 1)
        cond2, week2 = group2.rsplit('_W', 1)

        # Get TPI values and corresponding weights from marker SDs
        group1_tpi = tpi_df[(tpi_df['Week'] == int(week1)) &
                           (tpi_df['Condition'] == cond1)]
        group2_tpi = tpi_df[(tpi_df['Week'] == int(week2)) &
                           (tpi_df['Condition'] == cond2)]

        # Get corresponding marker data for weights
        group1_marker = marker_data[(marker_data['Week'] == int(week1)) &
                                  (marker_data['Condition'] == cond1)]
        group2_marker = marker_data[(marker_data['Week'] == int(week2)) &
                                  (marker_data['Condition'] == cond2)]

        if len(group1_tpi) > 0 and len(group2_tpi) > 0:
            # Get weights from marker SDs (inverse of variance)
            weights1 = 1 / (group1_marker[marker_sd_col].values ** 2)
            weights2 = 1 / (group2_marker[marker_sd_col].values ** 2)

            # Calculate weighted means
            weighted_mean1 = np.average(group1_tpi['TPI'], weights=weights1)
            weighted_mean2 = np.average(group2_tpi['TPI'], weights=weights2)

            # Calculate weighted variances
            weighted_var1 = np.average((group1_tpi['TPI'] - weighted_mean1) ** 2, weights=weights1)
            weighted_var2 = np.average((group2_tpi['TPI'] - weighted_mean2) ** 2, weights=weights2)

            # Calculate pooled SE
            se = np.sqrt(weighted_var1 + weighted_var2)

            if se == 0:
                p_value = 1.0
            else:
                # Calculate t-statistic
                t_stat = (weighted_mean1 - weighted_mean2) / se
                # Use conservative df = min(n1, n2) - 1
                df = min(len(group1_tpi), len(group2_tpi)) - 1
                p_value = 2 * (1 - stats.t.cdf(abs(t_stat), df))

            comprehensive_matrix.loc[group1, group2] = p_value
            comprehensive_matrix.loc[group2, group1] = p_value
            results.append(f"{group1} vs {group2}: p-value = {p_value:.6f}")

    results.extend([
        "\nComprehensive P-value Matrix:",
        comprehensive_matrix.to_string(),
        "\nWEEK-BY-WEEK ANALYSIS",
        "====================="
    ])

    # Create comprehensive heatmap
    plt.figure(figsize=(15, 13))
    mask = np.triu(np.ones_like(comprehensive_matrix, dtype=bool), k=1)

    sns.heatmap(comprehensive_matrix.astype(float),
                mask=mask,
                annot=True,
                cmap='coolwarm_r',
                vmin=0,
                vmax=1,
                fmt='.6f',
                linewidths=0.5,
                square=True)

    plt.title(f'Comprehensive P-value Analysis\n{marker} TPI',
             fontsize=FONT_SIZE['title'])
    plt.xticks(rotation=45, ha='right', fontsize=FONT_SIZE['tick_labels'])
    plt.yticks(rotation=0, fontsize=FONT_SIZE['tick_labels'])

    plt.tight_layout()

    # Save comprehensive heatmap
    comp_heatmap_path = output_dir / f'TPI_{marker}_comprehensive_pvalues.png'
    plt.savefig(str(comp_heatmap_path), dpi=300, bbox_inches='tight')
    plt.savefig(str(output_dir / f'TPI_{marker}_comprehensive_pvalues.svg'),
                format='svg', bbox_inches='tight')
    plt.close()

    # Save results to a text file
    results_path = output_dir / f'TPI_{marker}_comprehensive_stats.txt'
    with open(results_path, 'w') as f:
        f.write(f"Statistical Analysis for {marker} TPI Values\n")
        f.write("\n".join(results))

    return results

def main():
    # Skip mounting if already mounted
    try:
        drive.mount('/content/drive')
    except Exception as e:
        logger.info("Drive already mounted")

    # Get metadata path from the previously processed folder
    folder_path = os.getenv('PROCESSED_FOLDER_PATH')

    if not folder_path:
        logger.error("Previous folder path not found! Please run the SVG processing script first.")
        return

    base_path = Path(folder_path)
    metadata_file = base_path / 'metadata.csv'

    if not metadata_file.exists():
        logger.error(f"Metadata file not found in: {folder_path}")
        return

    logger.info(f"Found metadata.csv in: {folder_path}")

    # Create TPI plots output directory
    output_dir = base_path / 'TPI-Analysis (Regression of Slide data)'
    output_dir.mkdir(exist_ok=True)

    logger.info("Loading metadata...")
    metadata_df = pd.read_csv(metadata_file)

    logger.info("Creating TPI plots...")
    create_tpi_plots(metadata_df, output_dir)

    logger.info(f"TPI analysis complete. Results saved in {output_dir}")
    return output_dir

if __name__ == "__main__":
    main()

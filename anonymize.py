import pydicom
import os
import zipfile
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from PIL import Image
import hashlib

def anon_callback(ds, element):
    names_to_remove = [
        'SOP Instance UID',
        'Study Time',
        'Series Time',
        'Content Time',
        'Study Instance UID',
        'Series Instance UID',
        'Private Creator',
        'Media Storage SOP Instance UID',
        'Implementation Class UID'
    ]
    
    names_to_anon_time = [
        'Study Time',
        'Series Time',
        'Content Time',
    ]
    
    if element.name in names_to_remove:
        if element.VR == "UI":
            element.value = pydicom.uid.generate_uid()
        elif element.VR == "TM" and element.name in names_to_anon_time:
            element.value = "000000"  # set time to zeros
        else:
            element.value = "anon"

    if element.VR == "DA":
        date = element.value
        date = date[0:4] + "0101"  # set all dates to YYYY0101
        element.value = date

    if element.VR == "TM" and element.name not in names_to_anon_time:
        element.value = "000000"  # set time to zeros





def dicom_media_type( dataset ):
    type = str( dataset.file_meta[0x00020002].value )
    if type == '1.2.840.10008.5.1.4.1.1.6.1': # single ultrasound image
        return 'image'
    elif type == '1.2.840.10008.5.1.4.1.1.3.1': # multi-frame ultrasound image
        return 'multi'
    else:
        return 'other' # something else

def extract_single_zip_file(file_name, output_folder):
    if os.path.exists(output_folder):
        return

    try:
        zip_ref = zipfile.ZipFile(file_name)  # create zipfile object
        zip_ref.extractall(output_folder)  # extract file to dir
        zip_ref.close()  # close file
    except Exception as e:
        print(f'Skipping Bad Zip File: {file_name}. Exception: {e}')

def unzip_files_in_directory(directory_path, target_dir):
    os.makedirs(target_dir, exist_ok=True)

    if len(os.listdir(directory_path)) == 0:
        print("No zip files found")
        return

    print("Unzipping Files")

    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(
                extract_single_zip_file, 
                os.path.join(directory_path, item), 
                os.path.join(target_dir, os.path.splitext(item)[0])
            ) 
            for item in os.listdir(directory_path) 
            if item.endswith('.zip')
        ]

        for future in tqdm(as_completed(futures), total=len(futures), desc=""):
            try:
                future.result()
            except Exception as exc:
                print(f'An exception occurred: {exc}')


def deidentify_dicom( ds ):
    ds.remove_private_tags() # take out private tags added by notion or otherwise

    ds.file_meta.walk(anon_callback)
    ds.walk(anon_callback)

    media_type = ds.file_meta[0x00020002]
    is_video = str(media_type).find('Multi-frame')>-1
    is_secondary = str(media_type).find('Secondary')>-1
    if is_secondary:
        y0 = 101
    else:
        if (0x0018, 0x6011) in ds:
            y0 = ds['SequenceOfUltrasoundRegions'][0]['RegionLocationMinY0'].value
        else:
            y0 = 101

    if 'OriginalAttributesSequence' in ds:
        del ds.OriginalAttributesSequence
        
        
    # Check if Pixel Data is compressed
    if ds.file_meta.TransferSyntaxUID.is_compressed:
        # Attempt to decompress the Pixel Data
        try:
            ds.decompress()
        except NotImplementedError as e:
            print(f"Decompression not implemented for this transfer syntax: {e}")
            return None  # or handle this appropriately for your use case
        except Exception as e:
            print(f"An error occurred during decompression: {e}")
            return None  # or handle this appropriately for your use case

    # crop patient info above US region 
    arr = ds.pixel_array
        
        
    if is_video:
        arr[:,:y0] = 0
    else:
        arr[:y0] = 0
    
    
    # Update the Pixel Data
    ds.PixelData = arr.tobytes()
    
    ds.file_meta.TransferSyntaxUID = ds.file_meta.TransferSyntaxUID

    return ds




def create_dcm_filename( ds ):
    patient_id = ds.PatientID.rjust(8,'0')
    accession_number = ds.AccessionNumber.rjust(8,'0')

    media_type = ds.file_meta[0x00020002]
    is_video = str(media_type).find('Multi-frame')>-1
    is_secondary = str(media_type).find('Secondary')>-1

    if is_video:
        media = 'video'
    elif is_secondary:
        media = 'second'
    else:
        media = 'image'
        
    # Create a hash object
    hash_obj = hashlib.sha256()
    hash_obj.update(ds.pixel_array)
    
    image_hash = hash_obj.hexdigest()
    
    filename = f'{media}_{patient_id}_{accession_number}_{image_hash}.dcm'

    return filename


def deidentify_dcm_files(directory_path, target_directory, save_png=False):
    unzipped_path = os.path.join(directory_path, 'unzipped_dicoms')
    os.makedirs(target_directory, exist_ok=True)
    
    if save_png:
        png_directory = os.path.join(directory_path, 'png_debug')
        os.makedirs(png_directory, exist_ok=True)
    
    # First, collect all the DICOM file paths
    dicom_files = []
    for root, dirs, files in os.walk(unzipped_path):
        for file in files:
            if file.lower().endswith(".dcm"):
                dicom_files.append(os.path.join(root, file))
    
    for dicom_file in tqdm(dicom_files, total=len(dicom_files), desc="Processing DICOM files", unit="file"):
        # Read the DICOM file
        dataset = pydicom.dcmread(dicom_file)

        # Check media type and additional conditions
        media_type = dicom_media_type(dataset)
        if (media_type == 'image' and (0x0018, 0x6011) in dataset) or media_type == 'multi':
            
            # De-identify the DICOM dataset
            dataset = deidentify_dicom(dataset)
            
            #print(dataset)
            
            # Create a new filename
            new_filename = create_dcm_filename(dataset)
            
            # Set the target path to write the DICOM file
            target_path = os.path.join(target_directory, new_filename)

            # Make sure target directory exists
            if not os.path.exists(os.path.dirname(target_path)):
                os.makedirs(os.path.dirname(target_path))
            
            try:
                # Write the DICOM dataset to a new DICOM file
                dataset.save_as(target_path)
            except Exception as e:
                print(f"An error occurred while saving the file: {e}")

            # If save_png flag is True, save the image data as a PNG file
            if save_png:
                try:
                    # Convert the Pixel Array data to a PIL Image object
                    image = Image.fromarray(dataset.pixel_array)

                    # Save the Image object as a PNG file
                    png_file_path = os.path.join(png_directory, new_filename.replace('.dcm', '.png'))
                    image.save(png_file_path, "PNG")
                except Exception as e:
                    print(f"An error occurred while saving the PNG file: {e}")


main_dir = 'D:/DATA/CASBUSI/new_batch_(delete_me)'
zipped_dicom_path = f'{main_dir}/zip_files'
unzipped_dicom_path = f'{main_dir}/unzipped_dicoms'
deidentified_path = f'{main_dir}/deidentified'



# Unzip everything
unzip_files_in_directory(zipped_dicom_path, unzipped_dicom_path)

# Deidentify everything
deidentify_dcm_files(main_dir, deidentified_path, save_png=True)
